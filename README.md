# Mira

Mira: a voice AI agent that calls property enquiry leads, pitches flats in Hinglish, and books site visits.

Mira phones a customer who enquired about **Mahima Palm Springs** (a premium gated township on Tonk Road, Jaipur) through a Facebook ad. She checks the name, asks whether the customer prefers Hindi or English, confirms it is a good time, gives a short pitch, books a site visit in one of four daily slots, and ends the call politely. She only uses the project facts she was given; anything else gets a senior expert callback.

## Layout

| Path | What it is |
| --- | --- |
| `prompt/mira_prompt.md` | The prompt: background, global rules, guardrails, call script, FAQs. Used on every platform. |
| `config/settings.json` | Everything else: voice, transcriber, model, first message, call limits, silence and interruption behaviour. |
| `scripts/sync.py` | Creates or updates Mira on the chosen platform, then checks the live agent matches the files. |
| `tests/test_cases.md` | Test cases and results. |
| `recordings/` | Call recordings. |

## Keys

Copy `.env.example` to `.env` and fill in the values. `.env` is never committed.

- `PLATFORM`: `vapi` or `bolna`.
- `VAPI_API_KEY` (private key), plus an optional spare account in `VAPI_API_KEY2`. `VAPI_ACCOUNT=auto` uses the main key and falls back to the spare one if the main key fails; set it to `spare` to force the spare account (for example when the main account is out of credits).
- `BOLNA_API_KEY`.
- Agent IDs (`VAPI_ASSISTANT_ID`, `VAPI_ASSISTANT_ID2`, `BOLNA_AGENT_ID`) are written by the sync script the first time it creates an agent.

## Sync

```
python3 scripts/sync.py            # deploy to the platform in PLATFORM
python3 scripts/sync.py --check    # only check the live agent against the files
```

Change `PLATFORM` in `.env` to switch between Vapi and Bolna, then run the same command. Python 3 only, no extra packages.

On Vapi the script also deletes any stale dashboard draft (the dashboard keeps its own saved copy and would otherwise test an old prompt). Close the Mira tab in the Vapi dashboard before syncing and reload it afterwards.

## How the voice handles Hindi and English

The prompt is written in Roman Hinglish, as the brief asks. When Mira speaks in Hindi mode she writes Hindi words in Devanagari and English words (flat, price, numbers) in English letters; in English mode she uses English letters only. The voice (Azure `en-IN-AnanyaNeural`, an Indian English voice that also speaks Hindi) can then tell the languages apart, so English words are pronounced in English and Hindi words in Hindi. All numbers, prices, dates and times are spoken as English words.

## Ending a call

Every call ends with a closing line that finishes with "Have a nice day." The platform hangs up only after that line has been spoken (Vapi: end-call phrase; Bolna: hang-up check on the sign-off), so Mira never hangs up without saying goodbye.
