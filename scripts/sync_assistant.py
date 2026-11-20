"""Create or update the Mira assistant on Vapi from prompt/mira_prompt.md.

Usage: python3 scripts/sync_assistant.py
Reads VAPI_API_KEY (and VAPI_ASSISTANT_ID, if present) from .env.
"""

import json
import re
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
    greeting = re.search(r'Mira: "(Hello.*?)"', prompt).group(1)
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
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "mira-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())
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
        save_env_value("VAPI_ASSISTANT_ID", result["id"])
        print(f"Created assistant {result['id']} (saved to .env)")


if __name__ == "__main__":
    main()
