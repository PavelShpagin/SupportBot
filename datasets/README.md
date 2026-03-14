---
license: cc-by-4.0
task_categories:
  - text-classification
  - question-answering
language:
  - en
  - es
  - uk
tags:
  - technical-support
  - case-extraction
  - multilingual
  - chat
  - telegram
  - benchmark
pretty_name: SupportBench
size_categories:
  - 10K<n<100K
---

# SupportBench

A multilingual benchmark for evaluating **case extraction** from real-world tech support group chats.

SupportBench contains **60,000 messages** across **6 datasets** in **3 languages** (English, Spanish, Ukrainian), spanning **6 technical domains**. All messages are sourced from public Telegram support groups.

## Datasets

| Dataset | Language | Domain | Messages | Users | Reply% | Media |
|---------|----------|--------|----------|-------|--------|-------|
| Ardupilot-UA | Ukrainian | UAV / Drones | 10,000 | 319 | 51.8% | 1,440 |
| MikroTik-UA | Ukrainian | Networking | 10,000 | 205 | 55.4% | 884 |
| Domotica-ES | Spanish | Smart Home / HA | 10,000 | 530 | 58.3% | 736 |
| NASeros-ES | Spanish | NAS / Networking | 10,000 | 761 | 46.5% | 495 |
| Tasmota-EN | English | IoT Firmware | 10,000 | 1,237 | 31.5% | 903 |
| AdGuard-EN | English | Ad-blocking / VPN / DNS | 10,000 | 954 | 46.2% | 1,049 |

## Structure

```
SupportBench/
├── ua_ardupilot.json     # Ukrainian drone/UAV support
├── mikrotik_ua.json      # Ukrainian networking support
├── domotica_es.json      # Spanish smart home support
├── naseros.json          # Spanish NAS/networking support
├── tasmota.json          # English IoT firmware support
├── adguard_en.json       # English ad-blocking/VPN/DNS support
├── manifest.json         # Benchmark metadata and stats
└── README.md
```

## Message Format

Each JSON file contains `{"meta": {...}, "messages": [...]}`. Each message:

```json
{
  "id": "tg_tasmota_12345",
  "group_id": "tasmota",
  "ts": 1700000000000,
  "sender": "user_a1b2c3d4e5",
  "body": "My Sonoff Basic won't flash via serial...",
  "reply_to_id": "tg_tasmota_12340",
  "grouped_id": null,
  "media_type": "photo",
  "media_path": null,
  "webpage_url": null,
  "reactions": null,
  "views": 42,
  "forwards": 0
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique message ID (`tg_{group}_{telegram_id}`) |
| `group_id` | string | Dataset/group name |
| `ts` | int | Unix timestamp in milliseconds |
| `sender` | string | Anonymized sender (`user_{sha256[:10]}`) |
| `body` | string | Message text with Unicode emoji preserved |
| `reply_to_id` | string\|null | ID of parent message |
| `grouped_id` | int\|null | Album group ID for multi-media posts |
| `media_type` | string\|null | `photo`, `video`, `image`, `document`, `pdf`, `archive`, `audio`, `webpage`, `poll` |
| `media_path` | string\|null | Relative path to media file (only in `ua_ardupilot`) |
| `webpage_url` | string\|null | URL from link preview |
| `reactions` | object\|null | Emoji reaction counts (only in `ua_ardupilot`) |
| `views` | int\|null | View count |
| `forwards` | int\|null | Forward count |

## Notes

- All sender IDs are irreversibly anonymized via SHA-256 hashing
- `ua_ardupilot` has richer metadata (reactions, media paths) from a separate export pipeline
- AdGuard EN is a topics-based supergroup; `reply_to_id` often points to the topic root rather than the actual parent message (46.2% resolved within 10K window)
- Tasmota spans 3.5 years of IoT firmware support history (1,274 days)

## Intended Use

- **Case extraction**: identify problem-solution pairs from unstructured chat streams
- **Thread reconstruction**: reconstruct conversation threads from reply chains
- **Cross-lingual transfer**: evaluate whether case-mining generalizes across languages
- **Q&A retrieval**: mine question-answer pairs from technical discussions

## Citation

```bibtex
@inproceedings{supportbench2026,
  title={SupportBot: Continuous Case Mining for Grounded Technical Support},
  author={Shpagin, Pavel},
  booktitle={Proceedings of EMNLP 2026},
  year={2026}
}
```

## License

CC BY 4.0. All messages are from public Telegram groups. Sender identities are irreversibly anonymized.
