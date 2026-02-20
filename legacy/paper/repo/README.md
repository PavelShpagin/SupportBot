# SupportBot Repository

This repository contains the source code for **SupportBot**, as described in the paper "SupportBot: A Self-Learning Support Agent with Dynamic Case Mining from Chat Streams".

## Structure

- `supportbot/`: Python package containing the bot logic.
  - `agent.py`: Core agent with Gate -> Retrieve -> Respond pipeline.
  - `ingest.py`: Data ingestion for documentation and cases.
  - `storage.py`: Local vector store and blob storage implementation.
- `data/`: Data directory.
  - `docs/`: Vector store for documentation.
  - `cases/`: Vector store for mined cases.
  - `blobs/`: Storage for full content (HTML, etc.).

## Setup

1. Install dependencies:
   ```bash
   pip install google-generativeai beautifulsoup4 requests numpy
   ```

2. Set your Google API Key:
   ```bash
   export GOOGLE_API_KEY="your_api_key"
   ```
   (Note: The code uses `gemini-2.0-flash` and `models/gemini-embedding-001`. Ensure your key has access.)

## Usage

### 1. Ingest Data

Run the ingestion script to crawl documentation and ingest solved cases:

```bash
python supportbot/ingest.py
```

This will populate `data/docs` and `data/cases`.

### 2. Run the Bot

Run the agent in interactive mode:

```bash
python supportbot/agent.py
```

Or run the test script:

```bash
python supportbot/test_agent.py
```

## Architecture

The bot implements the **Extract-First** architecture:
1. **Gate**: Classifies messages (New Question, Ongoing, Statement, Noise).
2. **Retrieve**: Fetches relevant documentation and past solved cases.
3. **Respond**: Generates an answer using the retrieved context, with citations.

## License

MIT
