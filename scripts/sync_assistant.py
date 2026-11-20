"""Create or update the Mira assistant on Vapi from prompt/mira_prompt.md.

Usage: python3 scripts/sync_assistant.py
Reads VAPI_API_KEY (and VAPI_ASSISTANT_ID, if present) from .env.
"""

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
PROMPT_FILE = ROOT / "prompt" / "mira_prompt.md"
API_URL = "https://api.vapi.ai/assistant"
DEMO_CUSTOMER_NAME = "Rahul"

# Voice: Cartesia, Hindi female voice (handles Hinglish in Roman script).
VOICE_ID = "95d51f79-c397-46f9-b49a-23763d3eaa2d"  # Arushi - Hinglish Speaker

# Vapi defaults plus common Hindi equivalents.
ACKNOWLEDGEMENT_PHRASES = [
    "i understand", "i see", "i got it", "right", "okay", "ok", "sure",
    "alright", "got it", "yeah", "yes", "uh-huh", "mm-hmm", "mhmm", "hmm",
    "haan", "haan ji", "ji", "achha", "accha", "theek hai",
    "हां", "हाँ", "हाँ जी", "जी", "अच्छा", "ठीक है", "हम्म",
]
INTERRUPTION_PHRASES = [
    "stop", "wait", "hold", "no", "nope", "but", "actually", "enough",
    "ruko", "ruk", "nahi", "nahin", "suniye", "ek minute",
    "रुको", "रुकिए", "नहीं", "सुनिए", "एक मिनट",
]
SILENCE_REPROMPT = (
    "The customer has been silent. In the language mode you are using, "
    "ask once, in one short sentence, whether they can hear you."
)
SILENCE_GOODBYE = (
    "The customer is still silent. In the language mode you are using, "
    "say one short, polite goodbye sentence."
)


def read_env():
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def save_env_value(key, value):
    lines = ENV_FILE.read_text().splitlines()
    lines = [line for line in lines if not line.startswith(key + "=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def build_assistant():
    prompt = PROMPT_FILE.read_text().replace("{{customer_name}}", DEMO_CUSTOMER_NAME)
    # The greeting is the first Mira line under "Step One: Greeting Message".
    step_one = prompt.split("Step One: Greeting Message", 1)[1]
    greeting = re.search(r'Mira: "(.*?)"', step_one).group(1)
    return {
        "name": "Mira",
        "firstMessage": greeting,
        "firstMessageMode": "assistant-speaks-first",
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "multi",
        },
        "model": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "temperature": 0.4,
            "messages": [{"role": "system", "content": prompt}],
            "tools": [{"type": "endCall"}],
        },
        "voice": {
            "provider": "cartesia",
            "voiceId": VOICE_ID,
            "model": "sonic-3",
            "language": "hi",
        },
        "artifactPlan": {"recordingEnabled": True},
        "maxDurationSeconds": 300,
        # Safety net only; the silence hooks below handle silence first.
        "silenceTimeoutSeconds": 30,
        # Filter background noise and background voices (Krisp).
        "backgroundSpeechDenoisingPlan": {"smartDenoisingPlan": {"enabled": True}},
        # Interruptions: short acknowledgements don't cut Mira off, clear
        # interruptions ("stop", "ruko", "nahi") do. Hindi in both scripts,
        # since the transcriber may return Devanagari.
        "stopSpeakingPlan": {
            "numWords": 1,
            "voiceSeconds": 0.3,
            "backoffSeconds": 1,
            "acknowledgementPhrases": ACKNOWLEDGEMENT_PHRASES,
            "interruptionPhrases": INTERRUPTION_PHRASES,
        },
        # Hang up on voicemail without leaving a message (phone calls only).
        "voicemailDetection": {"provider": "vapi", "type": "audio"},
        # Silence: check once after 10 seconds, end politely after 20.
        "hooks": [
            {
                "name": "silence_reprompt",
                "on": "customer.speech.timeout",
                "options": {"timeoutSeconds": 10, "triggerMaxCount": 1},
                "do": [{"type": "say", "prompt": SILENCE_REPROMPT}],
            },
            {
                "name": "silence_end",
                "on": "customer.speech.timeout",
                "options": {"timeoutSeconds": 20, "triggerMaxCount": 1},
                "do": [
                    {"type": "say", "prompt": SILENCE_GOODBYE},
                    {"type": "tool", "tool": {"type": "endCall"}},
                ],
            },
        ],
    }


def call_api(method, url, api_key, body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mira-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Vapi error {error.code}: {error.read().decode()}")


def main():
    env = read_env()
    api_key = env.get("VAPI_API_KEY")
    if not api_key:
        raise SystemExit("VAPI_API_KEY is missing from .env")

    assistant = build_assistant()
    assistant_id = env.get("VAPI_ASSISTANT_ID")

    if assistant_id:
        call_api("PATCH", f"{API_URL}/{assistant_id}", api_key, assistant)
        print(f"Updated assistant {assistant_id}")
    else:
        result = call_api("POST", API_URL, api_key, assistant)
        assistant_id = result["id"]
        save_env_value("VAPI_ASSISTANT_ID", assistant_id)
        print(f"Created assistant {assistant_id} (saved to .env)")

    # The Vapi dashboard keeps its own "Draft" copy of the assistant. When a
    # dashboard tab is open while the assistant changes, the tab saves its old
    # copy as a draft and sends it with every test call. Delete all drafts so
    # the dashboard loads the published version instead.
    delete_drafts(assistant_id, api_key)
    time.sleep(10)
    if delete_drafts(assistant_id, api_key):
        print("WARNING: an open Vapi dashboard tab re-saved an old draft.")
        print("Close the Mira tab in the dashboard, then run this script again.")

    # Check what the dashboard will load: no draft, and the published
    # version matching the file.
    expected_prompt = assistant["model"]["messages"][0]["content"]
    live = call_api("GET", f"{API_URL}/{assistant_id}", api_key, None)
    check(live, assistant, expected_prompt, "Live assistant")
    version = live.get("latestVersion")
    if version:
        published = call_api("GET", f"{API_URL}/{assistant_id}/versions/{version}", api_key, None)
        check(published.get("data", published), assistant, expected_prompt, f"Published version {version}")
    if call_api("GET", f"{API_URL}/{assistant_id}/draft", api_key, None).get("results"):
        raise SystemExit("A dashboard draft still exists; the dashboard would use it")
    print("No dashboard draft left")
    print(f"Opening line: {live['firstMessage']}")
    print("Reload the Vapi dashboard page before testing.")


def delete_drafts(assistant_id, api_key):
    drafts = call_api("GET", f"{API_URL}/{assistant_id}/draft", api_key, None)
    found = drafts.get("results", [])
    for draft in found:
        call_api("DELETE", f"{API_URL}/{assistant_id}/draft/{draft['id']}", api_key, None)
        print(f"Deleted dashboard draft {draft['id']}")
    return bool(found)


def check(found, assistant, expected_prompt, label):
    prompt_ok = found["model"]["messages"][0]["content"] == expected_prompt
    greeting_ok = found.get("firstMessage") == assistant["firstMessage"]
    if not (prompt_ok and greeting_ok):
        raise SystemExit(f"{label} does NOT match prompt/mira_prompt.md")
    print(f"{label} matches the file")


if __name__ == "__main__":
    main()
