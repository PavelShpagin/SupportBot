---
license: cc-by-nc-4.0
task_categories:
  - question-answering
  - text-generation
language:
  - uk
  - es
  - en
tags:
  - technical-support
  - case-mining
  - multilingual
  - multimodal
  - chat
  - telegram
  - emoji
  - reactions
size_categories:
  - 10K<n<100K
---

# SupportBench

A multilingual, multimodal benchmark for evaluating technical support case-mining systems.

## Overview

SupportBench contains **60,000 messages** from **6 public Telegram technical support groups** spanning **3 languages** (Ukrainian, Spanish, English) and **6 technical domains**. Each dataset includes full message text with Unicode emoji, reply chains, downloaded media files, emoji reaction breakdowns, album grouping, and anonymized sender identifiers.

## Datasets

| Dataset | Language | Domain | Messages | Users | Reply% | Media | Q&A | Reactions |
|---------|----------|--------|----------|-------|--------|-------|-----|-----------
| Ardupilot-UA | Ukrainian | UAV / Drone Systems | 10,000 | 319 | 51.4% | 1,134 | 1,115 | 1,885 |
| SelfHost-UA | Ukrainian | Self-hosting / Docker | 10,000 | 224 | 46.5% | 746 | 1,170 | 3,247 |
| Domotica-ES | Spanish | Smart Home / HA | 10,000 | 530 | 58.4% | 723 | 1,661 | 1,279 |
| NASeros-ES | Spanish | NAS / Networking | 10,000 | 764 | 46.3% | 494 | 1,342 | 1,438 |
| LineageOS-EN | English | Mobile OS / Custom ROM | 10,000 | 559 | 50.0% | 734 | 807 | 1,222 |
| Tasmota-EN | English | IoT / Firmware | 10,000 | 784 | 33.4% | 951 | 1,134 | 1,155 |
| **Total** | **3** | **6** | **60,000** | **3,180** | **— ** | **4,782** | **7,229** | **10,226** |

**Note:** NASeros uses Telegram's forum/topics feature. Tasmota spans 3.5 years of IoT firmware support history (1,274 days).

## Structure

```
SupportBench/
├── ua_ardupilot.json          # Unified format (messages + metadata)
├── ua_ardupilot/
│   └── media/                 # Downloaded media files
│       ├── 12345.jpg
│       ├── 12400.mp4
│       └── ...
├── ua_selfhosted.json
├── ua_selfhosted/media/
├── domotica_es.json
├── domotica_es/media/
├── naseros.json
├── naseros/media/
├── lineageos.json
├── lineageos/media/
├── tasmota.json
├── tasmota/media/
├── manifest.json              # Benchmark metadata
├── stats.json                 # Computed statistics
└── README.md
```

## Message Format

Each unified JSON file contains `{"meta": {...}, "messages": [...]}`. Each message:

```json
{
  "id": "tg_ua_ardupilot_17254",
  "group_id": "ua_ardupilot",
  "ts": 1741523400000,
  "sender": "user_3f0a477baf",
  "body": "MNT1_ROLL_MIN/MAX або діапазон серви, залежно скільки фізично може гімбал повернути градусів.",
  "reply_to_id": "tg_ua_ardupilot_17253",
  "grouped_id": null,
  "media_type": "photo",
  "media_path": "media/108500.jpg",
  "webpage_url": null,
  "reactions": {"👍": 5, "❤": 2, "🔥": 1},
  "views": 150,
  "forwards": 0
}
```

### Field descriptions

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique message ID (`tg_{group}_{telegram_id}`) |
| `group_id` | string | Dataset/group name |
| `ts` | int | Unix timestamp in milliseconds |
| `sender` | string | Anonymized sender (SHA-256 hash, `user_{hash[:10]}`) |
| `body` | string | Message text with Unicode emoji preserved |
| `reply_to_id` | string\|null | ID of parent message (may reference messages outside the export window) |
| `grouped_id` | int\|null | Album group ID — messages sharing this ID form a multi-media album |
| `media_type` | string\|null | `photo`, `video`, `image`, `document`, `pdf`, `archive`, `audio`, `webpage`, `poll`, or `null` |
| `media_path` | string\|null | Relative path to downloaded media file, or `null` if not downloaded (files >20MB skipped) |
| `webpage_url` | string\|null | URL from link preview, if present |
| `reactions` | object | Emoji reaction breakdown: `{"👍": 5, "❤": 2}` |
| `views` | int\|null | View count |
| `forwards` | int\|null | Forward count |

### Emoji and reactions

- **Text emoji**: Preserved as Unicode in the `body` field (e.g., `"працює! 🎉"`)
- **Reactions**: Full emoji-to-count mapping. Top reactions across all datasets: 👍, ❤, 😁, 🔥, 💯
- **Albums**: Multi-photo posts share a `grouped_id` — group by this field to reconstruct photo albums

## Reproducing SupportBench

```bash
# 1. Export messages + media from Telegram (requires Telethon session)
python scripts/export_telegram_media.py

# 2. Build unified format with production-style IDs and stats
python scripts/build_supportbench_unified.py

# 3. Upload to HuggingFace
python scripts/upload_supportbench_hf.py
```

## Intended Use

SupportBench is designed for evaluating:
- **Case extraction**: Can a system identify problem-solution pairs from unstructured chat streams?
- **Support response generation**: Can a system answer technical questions using mined cases?
- **Cross-lingual transfer**: Do case-mining techniques generalize across languages?
- **Multimodal understanding**: Can systems leverage screenshots and hardware photos for diagnosis?
- **Reaction signals**: Can emoji reactions (👍, ❤) serve as weak supervision for solved-case detection?

## Citation

```bibtex
@inproceedings{supportbench2026,
  title={SupportBot: Continuous Case Mining for Grounded Technical Support with the SupportBench Benchmark},
  author={Anonymous},
  booktitle={Proceedings of the 2026 Conference on Empirical Methods in Natural Language Processing},
  year={2026}
}
```

## License

CC BY-NC 4.0. For research use only. All messages are from public Telegram groups. Sender identities are irreversibly anonymized via SHA-256 hashing.
