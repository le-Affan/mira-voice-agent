"""Create or update Mira on Vapi or Bolna, then check that the live agent matches.

Usage:
    python3 scripts/sync.py                     # platform from PLATFORM in .env (default: vapi)
    python3 scripts/sync.py --platform bolna    # override the platform for one run
    python3 scripts/sync.py --check             # only check the live agent, change nothing

The prompt comes from prompt/mira_prompt.md and every other setting from config/settings.json.
Keys and agent IDs live in .env (see .env.example).
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
PROMPT_FILE = ROOT / "prompt" / "mira_prompt.md"
SETTINGS_FILE = ROOT / "config" / "settings.json"

VAPI_URL = "https://api.vapi.ai"
BOLNA_URL = "https://api.bolna.ai"


# ---------- shared helpers ----------

def read_env():
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def save_env_value(key, value):
    lines = [line for line in ENV_FILE.read_text().splitlines() if not line.startswith(key + "=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status


def call_api(method, url, key, body=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "mira-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise ApiError(error.code, error.read().decode(errors="replace")) from None


def load_inputs():
    settings = json.loads(SETTINGS_FILE.read_text())
    name = settings["customer_name"]
    prompt = PROMPT_FILE.read_text().replace("{{customer_name}}", name)
    first_message = settings["first_message"].replace("{customer_name}", name)
    return settings, prompt, first_message


class Checker:
    def __init__(self):
        self.failures = []

    def expect(self, label, ok):
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        if not ok:
            self.failures.append(label)

    def finish(self):
        if self.failures:
            raise SystemExit(f"Live agent does NOT match the files: {', '.join(self.failures)}")
        print("Live agent matches prompt/mira_prompt.md and config/settings.json.")


# ---------- Vapi ----------

def vapi_assistant(settings, prompt, first_message):
    call, voice, stt, llm = settings["call"], settings["voice"], settings["transcriber"], settings["llm"]
    sign_off = settings["sign_off"]
    return {
        "name": settings["agent_name"],
        "firstMessage": first_message,
        "firstMessageMode": "assistant-speaks-first",
        "firstMessageInterruptionsEnabled": False,
        "transcriber": {"provider": stt["provider"], "model": stt["model"], "language": stt["language"]},
        "model": {
            "provider": llm["provider"],
            "model": llm["model"],
            "temperature": llm["temperature"],
            "maxTokens": llm["max_tokens"],
            "messages": [{"role": "system", "content": prompt}],
            # No end-call tool: the call ends only after the spoken sign-off (endCallPhrases),
            # so Mira can never hang up without saying goodbye.
            "tools": [],
        },
        "voice": {"provider": voice["provider"], "voiceId": voice["voice_id"], "speed": voice["speed"]},
        "endCallPhrases": [sign_off.lower(), "हैव अ नाइस डे"],
        "artifactPlan": {"recordingEnabled": call["recording"]},
        "maxDurationSeconds": call["max_duration_seconds"],
        "silenceTimeoutSeconds": call["silence_hangup_seconds"] + 10,
        "backgroundSpeechDenoisingPlan": {"smartDenoisingPlan": {"enabled": call["noise_cancellation"]}},
        "startSpeakingPlan": {"waitSeconds": 0.5},
        "stopSpeakingPlan": {
            "numWords": call["interruption_min_words"],
            "backoffSeconds": 1,
            "acknowledgementPhrases": settings["acknowledgement_phrases"],
            "interruptionPhrases": settings["interruption_phrases"],
        },
        "voicemailDetection": {"provider": "vapi", "type": "audio"} if call["voicemail_detection"] else "off",
        "hooks": [
            {
                "name": "silence_check",
                "on": "customer.speech.timeout",
                "options": {"timeoutSeconds": call["silence_check_seconds"], "triggerMaxCount": 1},
                "do": [{"type": "say", "prompt": settings["silence_check_prompt"]}],
            },
            {
                "name": "silence_end",
                "on": "customer.speech.timeout",
                "options": {"timeoutSeconds": call["silence_hangup_seconds"], "triggerMaxCount": 1},
                "do": [
                    {"type": "say", "prompt": settings["silence_end_prompt"]},
                    {"type": "tool", "tool": {"type": "endCall"}},
                ],
            },
        ],
    }


def vapi_account(env):
    """Pick the Vapi key: VAPI_ACCOUNT=primary|spare|auto (auto falls back to the spare key on failure)."""
    accounts = {
        "primary": (env.get("VAPI_API_KEY"), "VAPI_ASSISTANT_ID"),
        "spare": (env.get("VAPI_API_KEY2"), "VAPI_ASSISTANT_ID2"),
    }
    choice = env.get("VAPI_ACCOUNT", "auto").lower()
    if choice in accounts:
        key, id_name = accounts[choice]
        if not key:
            raise SystemExit(f"No key set for the {choice} Vapi account in .env")
        return choice, key, id_name
    for name in ("primary", "spare"):
        key, id_name = accounts[name]
        if not key:
            continue
        try:
            call_api("GET", f"{VAPI_URL}/assistant?limit=1", key)
            if name == "spare":
                print("Primary Vapi key failed, so the spare Vapi key (VAPI_API_KEY2) is being used.")
            return name, key, id_name
        except ApiError as error:
            print(f"The {name} Vapi key did not work ({error.status}).")
    raise SystemExit("Neither Vapi key works. Check VAPI_API_KEY and VAPI_API_KEY2 in .env.")


def vapi_delete_drafts(key, assistant_id):
    drafts = call_api("GET", f"{VAPI_URL}/assistant/{assistant_id}/draft", key).get("results", [])
    for draft in drafts:
        call_api("DELETE", f"{VAPI_URL}/assistant/{assistant_id}/draft/{draft['id']}", key)
        print(f"Deleted stale dashboard draft {draft['id']}")
    return bool(drafts)


def vapi_sync(env, check_only):
    settings, prompt, first_message = load_inputs()
    wanted = vapi_assistant(settings, prompt, first_message)
    account, key, id_name = vapi_account(env)
    assistant_id = env.get(id_name)
    print(f"Platform: Vapi ({account} account)")

    if not check_only:
        if assistant_id:
            call_api("PATCH", f"{VAPI_URL}/assistant/{assistant_id}", key, wanted)
            print(f"Updated assistant {assistant_id}")
        else:
            assistant_id = call_api("POST", f"{VAPI_URL}/assistant", key, wanted)["id"]
            save_env_value(id_name, assistant_id)
            print(f"Created assistant {assistant_id} (saved to .env as {id_name})")
        # The dashboard keeps its own draft copy and sends it with test calls; an open
        # dashboard tab can re-save an old one, so clear drafts twice.
        vapi_delete_drafts(key, assistant_id)
        time.sleep(8)
        if vapi_delete_drafts(key, assistant_id):
            print("WARNING: an open Vapi dashboard tab re-saved an old draft. Close it and run again.")
    elif not assistant_id:
        raise SystemExit(f"No {id_name} in .env yet; run without --check first.")

    live = call_api("GET", f"{VAPI_URL}/assistant/{assistant_id}", key)
    version = live.get("latestVersion")
    published = (call_api("GET", f"{VAPI_URL}/assistant/{assistant_id}/versions/{version}", key)
                 if version else live)
    published = published.get("data", published)
    drafts = call_api("GET", f"{VAPI_URL}/assistant/{assistant_id}/draft", key).get("results", [])

    c = Checker()
    for label, agent in (("live", live), (f"published {version}", published)):
        model = agent.get("model", {})
        c.expect(f"{label}: prompt matches file", model.get("messages", [{}])[0].get("content") == prompt)
        c.expect(f"{label}: first message matches", agent.get("firstMessage") == first_message)
    model = live.get("model", {})
    c.expect("model and temperature", model.get("model") == wanted["model"]["model"]
             and model.get("temperature") == wanted["model"]["temperature"])
    c.expect("no end-call tool (goodbye is always spoken first)", not model.get("tools") and not model.get("toolIds"))
    c.expect("end-call phrase set to the sign-off", settings["sign_off"].lower() in live.get("endCallPhrases", []))
    c.expect("voice", live.get("voice", {}).get("voiceId") == settings["voice"]["voice_id"])
    c.expect("transcriber", live.get("transcriber", {}).get("language") == settings["transcriber"]["language"])
    c.expect("interruption needs 2+ words", live.get("stopSpeakingPlan", {}).get("numWords") == settings["call"]["interruption_min_words"])
    c.expect("max call length", live.get("maxDurationSeconds") == settings["call"]["max_duration_seconds"])
    c.expect("recording on", live.get("artifactPlan", {}).get("recordingEnabled") is True)
    c.expect("voicemail detection", live.get("voicemailDetection") not in (None, "off"))
    c.expect("noise filtering", live.get("backgroundSpeechDenoisingPlan", {}).get("smartDenoisingPlan", {}).get("enabled") is True)
    c.expect("silence hooks", len(live.get("hooks", [])) == 2)
    c.expect("no stale dashboard draft", not drafts)
    c.finish()
    print(f"Assistant ID: {assistant_id}. Reload the Vapi dashboard before a test call.")


# ---------- Bolna ----------

def bolna_agent(settings, prompt, first_message, language):
    call, voice, stt, llm = settings["call"], settings["voice"], settings["transcriber_bolna"], settings["llm"]
    return {
        "agent_config": {
            "agent_name": settings["agent_name"],
            "agent_welcome_message": first_message,
            "tasks": [{
                "task_type": "conversation",
                "toolchain": {"execution": "sequential", "pipelines": [["transcriber", "llm", "synthesizer"]]},
                "tools_config": {
                    "llm_agent": {
                        "agent_type": "simple_llm_agent",
                        "agent_flow_type": "streaming",
                        "llm_config": {
                            "provider": llm["provider"],
                            "model": llm["model"],
                            "temperature": llm["temperature"],
                            "max_tokens": llm["max_tokens"],
                        },
                    },
                    "synthesizer": {
                        "provider": "azuretts",
                        "provider_config": {"voice": voice["voice_id"], "model": "neural",
                                            "language": voice["language"], "speed": voice["speed"]},
                        "stream": True,
                        "buffer_size": 150,
                        "audio_format": "wav",
                    },
                    "transcriber": {
                        "provider": stt["provider"],
                        "model": stt["model"],
                        "language": language,
                        "keywords": stt["keywords"],
                        "stream": True,
                        "encoding": "linear16",
                        "sampling_rate": 16000,
                    },
                    "input": {"provider": "plivo", "format": "wav"},
                    "output": {"provider": "plivo", "format": "wav"},
                },
                "task_config": {
                    "call_terminate": call["max_duration_seconds"],
                    "hangup_after_silence": call["silence_hangup_seconds"],
                    "check_if_user_online": True,
                    "trigger_user_online_message_after": call["silence_check_seconds"],
                    "check_user_online_message": settings["silence_check_message"],
                    "user_online_message_in_llm_history": True,
                    "number_of_words_for_interruption": call["interruption_min_words"],
                    "incremental_delay": 400,
                    # Prompt-based hang-up: fires only after Mira's sign-off has been spoken,
                    # and adds no extra goodbye of its own.
                    "hangup_after_LLMCall": True,
                    "call_cancellation_prompt": settings["hangup_check_prompt"],
                    "call_hangup_message": None,
                    "backchanneling": False,
                    "ambient_noise": False,
                    "noise_cancellation_level": 85 if call["noise_cancellation"] else None,
                    "voicemail": call["voicemail_detection"],
                },
            }],
        },
        "agent_prompts": {"task_1": {"system_prompt": prompt}},
    }


def bolna_sync(env, check_only):
    settings, prompt, first_message = load_inputs()
    key = env.get("BOLNA_API_KEY")
    if not key:
        raise SystemExit("BOLNA_API_KEY is missing from .env")
    agent_id = env.get("BOLNA_AGENT_ID")
    print("Platform: Bolna")

    if not check_only:
        language = settings["transcriber_bolna"]["language"]
        body = bolna_agent(settings, prompt, first_message, language)
        try:
            agent_id = bolna_write(key, agent_id, body)
        except ApiError as error:
            if "language" not in str(error).lower() or language == "hi":
                raise SystemExit(f"Bolna rejected the agent: {error}")
            # Bolna validates the transcriber language per model; fall back to Hindi,
            # which still handles English words mixed in.
            print(f"Bolna does not accept transcriber language '{language}', using 'hi'.")
            agent_id = bolna_write(key, agent_id, bolna_agent(settings, prompt, first_message, "hi"))
    elif not agent_id:
        raise SystemExit("No BOLNA_AGENT_ID in .env yet; run without --check first.")

    live = call_api("GET", f"{BOLNA_URL}/v2/agent/{agent_id}", key)
    task = live.get("tasks", [{}])[0]
    tools, task_config = task.get("tools_config", {}), task.get("task_config", {})
    live_prompt = live.get("agent_prompts", {}).get("task_1", {}).get("system_prompt")

    c = Checker()
    c.expect("prompt matches file", live_prompt == prompt)
    c.expect("first message matches", live.get("agent_welcome_message") == first_message)
    c.expect("model", tools.get("llm_agent", {}).get("llm_config", {}).get("model") == settings["llm"]["model"])
    c.expect("voice", tools.get("synthesizer", {}).get("provider_config", {}).get("voice") == settings["voice"]["voice_id"])
    c.expect("transcriber", tools.get("transcriber", {}).get("model") == settings["transcriber_bolna"]["model"])
    c.expect("hang up only after the sign-off", task_config.get("hangup_after_LLMCall") is True
             and settings["sign_off"] in (task_config.get("call_cancellation_prompt") or ""))
    c.expect("no extra hang-up message", not task_config.get("call_hangup_message"))
    c.expect("interruption needs 2+ words", task_config.get("number_of_words_for_interruption") == settings["call"]["interruption_min_words"])
    c.expect("max call length", task_config.get("call_terminate") == settings["call"]["max_duration_seconds"])
    c.expect("silence hang-up", task_config.get("hangup_after_silence") == settings["call"]["silence_hangup_seconds"])
    c.expect("voicemail detection", task_config.get("voicemail") is True)
    c.expect("noise cancellation", bool(task_config.get("noise_cancellation_level")))
    c.finish()
    print(f"Agent ID: {agent_id}. Bolna records every call by default.")


def bolna_write(key, agent_id, body):
    if agent_id:
        call_api("PUT", f"{BOLNA_URL}/v2/agent/{agent_id}", key, body)
        print(f"Updated agent {agent_id}")
        return agent_id
    agent_id = call_api("POST", f"{BOLNA_URL}/v2/agent", key, body)["agent_id"]
    save_env_value("BOLNA_AGENT_ID", agent_id)
    print(f"Created agent {agent_id} (saved to .env as BOLNA_AGENT_ID)")
    return agent_id


def main():
    parser = argparse.ArgumentParser(description="Sync Mira to Vapi or Bolna.")
    parser.add_argument("--platform", choices=["vapi", "bolna"], help="override PLATFORM from .env")
    parser.add_argument("--check", action="store_true", help="only check the live agent")
    args = parser.parse_args()

    env = read_env()
    platform = (args.platform or env.get("PLATFORM") or "vapi").lower()
    if platform == "vapi":
        vapi_sync(env, args.check)
    elif platform == "bolna":
        bolna_sync(env, args.check)
    else:
        raise SystemExit(f"Unknown PLATFORM '{platform}'. Use vapi or bolna.")


if __name__ == "__main__":
    main()
