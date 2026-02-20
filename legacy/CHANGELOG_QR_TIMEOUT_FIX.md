# QR Code Timeout Fix - Change Log

## Issue
The Signal device linking QR code was timing out too quickly, which wasn't enough time for users to:
1. See the QR code
2. Open their Signal app
3. Navigate to the linking section
4. Scan the QR code

**Two separate timeout issues were found:**
1. **signal-bot**: Hardcoded 180 seconds (3 minutes) timeout
2. **signal-ingest**: Hardcoded 120 seconds (2 minutes) timeout - **this was the actual failure point**

This resulted in "Timed out waiting for QR scan" errors, with the ingest service timing out before users could scan.

## Changes Made

### Signal Bot Service

#### 1. Configuration (`signal-bot/app/config.py`)
- Added new configuration field: `signal_link_timeout_seconds: int`
- Default value: 600 seconds (10 minutes)
- Minimum value: 60 seconds
- Configurable via environment variable: `SIGNAL_LINK_TIMEOUT_SECONDS`

#### 2. Main Application (`signal-bot/app/main.py`)
- Updated `LinkDeviceManager` initialization to use `settings.signal_link_timeout_seconds` instead of hardcoded `180`
- Now respects the configured timeout value

#### 3. Link Device Manager (`signal-bot/app/signal/link_device.py`)
- Improved error message to be more helpful
- Changed from: `"Timed out waiting for scan."`
- Changed to: `"QR code scan timed out after {elapsed} seconds. Please try again and scan the QR code more quickly."`
- Now shows the actual elapsed time to help users understand how long they took

#### 4. User Messages (`signal-bot/app/signal/signal_cli.py`)
- Updated Ukrainian message: Changed "60 секунд" to "10 хвилин"
- Updated English message: Changed "60 seconds" to "10 minutes"
- Users now see accurate timeout information

### Signal Ingest Service (History Bootstrap)

#### 5. Configuration (`signal-ingest/ingest/config.py`)
- Added new configuration field: `history_qr_timeout_seconds: float`
- Default value: 600 seconds (10 minutes)
- Minimum value: 60 seconds
- Configurable via environment variable: `HISTORY_QR_TIMEOUT_SECONDS`

#### 6. Main Worker (`signal-ingest/ingest/main.py`)
- Updated `_wait_for_desktop_linked` call to use `settings.history_qr_timeout_seconds` instead of hardcoded `120`
- **This was the critical fix** - the ingest service was timing out after 2 minutes

### Environment Configuration

#### 7. Environment Example (`env.example`)
- Added `SIGNAL_LINK_TIMEOUT_SECONDS=600` for signal-bot service
- Added `HISTORY_QR_TIMEOUT_SECONDS=600` for signal-ingest service
- Added clear documentation explaining the difference between:
  - `HISTORY_QR_TIMEOUT_SECONDS`: Time to wait for QR scan (10 minutes)
  - `HISTORY_MAX_SECONDS`: Time to wait for history sync after scan (5 minutes)
- Documented default and minimum values with helpful comments

## Benefits

1. **More time for users**: 10 minutes instead of 2 minutes gives users plenty of time to scan the QR code
2. **Configurable**: Admins can adjust both timeouts based on their needs
3. **Better error messages**: Users now see exactly how long they took and get actionable advice
4. **Accurate user communication**: Messages now correctly state "10 minutes" instead of outdated "60 seconds"
5. **Backwards compatible**: If environment variables are not set, defaults to 600 seconds (much better than the old 120/180)

## Usage

To customize the timeouts, add to your `.env` file:

```bash
# Signal bot timeout (for direct device linking)
SIGNAL_LINK_TIMEOUT_SECONDS=600

# Signal ingest timeout (for history bootstrap QR scan)
HISTORY_QR_TIMEOUT_SECONDS=600

# History sync timeout (AFTER QR scan completes)
HISTORY_MAX_SECONDS=300
```

## Testing

To test the fix:
1. Restart the services: `docker-compose restart signal-bot signal-ingest`
2. Navigate to the history bootstrap page
3. Request a QR code for a group
4. Take your time scanning it (you now have 10 minutes by default)
5. If you do exceed the timeout, you'll see a helpful error message with the elapsed time

## Related Files
- `signal-bot/app/config.py` - Bot configuration definition
- `signal-bot/app/main.py` - Bot LinkDeviceManager initialization
- `signal-bot/app/signal/link_device.py` - Bot timeout logic and error messages
- `signal-bot/app/signal/signal_cli.py` - User-facing messages
- `signal-ingest/ingest/config.py` - Ingest configuration definition
- `signal-ingest/ingest/main.py` - Ingest QR scan timeout logic
- `env.example` - Environment variable documentation
