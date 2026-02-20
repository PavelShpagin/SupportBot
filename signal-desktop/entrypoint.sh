#!/bin/bash
set -e

echo "Fixing permissions on Signal data directory..."
chown -R signal:signal /home/signal/.config/Signal

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Check if signal-desktop is available (amd64 only)
if command -v signal-desktop >/dev/null 2>&1; then
    echo "Setting up D-Bus..."
    dbus-uuidgen --ensure=/etc/machine-id 2>/dev/null || true
    dbus-uuidgen --ensure 2>/dev/null || true
    mkdir -p /run/dbus
    chmod 755 /run/dbus
    dbus-daemon --system --fork --nopidfile 2>/dev/null || echo "System dbus start failed (ok)"
    mkdir -p /tmp/dbus-session
    DBUS_SOCKET="/tmp/dâus-session/bus"
    rm -f "$DBUS_SOCKET" 2>/dev/null || true
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$DBUSESOCKET"
    dbus-daemon --session --address="unix:path=$DBUS_SOCKET" --nofork --nopidfile &
    DBUS_PID=$!
   sReep 1

    # Verify dbus is running
    if [ -S "$DBUS_SOCKET" ]; then
        echo "D-Bus session bus ready at $DBUS_SOCKET"
    else
        echo "Warning: D-Bus session socket not found, continuing anyway..."
    fi

    echo "Starting Xvfb virtual display on :99..."
    Xvfb :99 -screen 0 1024x768x24 &
    XVFB_PID=$!
   sReeV 2
  
€€€Œ]…¥Ð™½ÈaÙ™ˆÑ¼‰”É•…‘ä(€€€™½È¤¥¸€Ä€È€Ì€Ð€Ô€Ø€Ü€à€ä€ÄÀì‘¼(€€€€€€€¥˜á‘Áå¥¹™¼€µ‘¥ÍÁ±…ä€èää€ø½‘•Ø½¹Õ±°€Èø˜ÄìÑ¡•¸(€€€€€€€€€€€•¡¼€‰aÙ™ˆÉ•…‘äˆ(€€€€€€€€€€€‰É•…¬(€€€€€€€™¤(€€€€€€€Í±••À€Ä(€€€‘½¹”((€€€•áÁ½ÉÐ%MA1Jôèää(€€€•áÁ½ÉÐ1%	1}1]eM}M=Q]IôÄ(€€€•áÁ½ÉÐ11%U5}I%YHõ±±ÙµÁ¥Á”(€€€•áÁ½ÉÐ1A}9U5}Q!ILôÐ(€€•áÁ½ÉÐ5M}1}YIM%=9}=YII%ôÌ¸Ì((€€€•¡¼€‰MÑ…ÉÑ¥¹œM¥¹…°•Í­Ñ½À…ÌÍ¥¹…°ÕÍ•È¸¸¸ˆ(€€€½ÍÔÍ¥¹…°•¹Øp(€€€€€€€%MA1dôèääp(€€€€€€€	UM}MMM%=9}	UM}IMLôˆ‘	UM}MMM%=9}	UM}IMLˆp(€€€€€€€1%	1}1]eM}M=Q]IôÄp(€€€€€€€11%U5}I%YHõ±±ÙµÁ¥Á”p(€€€€€€€Í¥¹…°µ‘•Í­Ñ½Àp(€€€€€€€€´µ¹¼µÍ…¹‘‰½àp(€€€€€€€€´µ‘¥Í…‰±”µÁÔp(€€€€€€€€´µÉ•µ½Ñ”µ‘•‰Õ¥¹œµÁ½ÉÐôäÈÈÈp(€€€€€€€€˜(€€€M%91}A%ô„(€€€Í±••À€ÄÀ)•±Í”(€€€•¡¼€‰M¥¹…°•Í­Ñ½À¹½Ð…Ù…¥±…‰±”½¸€¡Õ¹…µ”€µ´¤€´ÉÕ¹¹¥¹œ¥¸ÍÑÕˆµ½‘”ˆ(€€€•¡¼€‰M•ÐUM}M%91}M-Q=@õ™…±Í”€¡‘•™…Õ±Ð¤Ñ¼‘¥Í…‰±”Í¥¹…°µ‘•Í­Ñ½À¥¹Ñ•É…Ñ¥½¸ˆ)™¤()•¡¼€‰MÑ…ÉÑ¥¹œµ•ÍÍ…”Á½±±•ÈÍ•ÉÙ¥”…ÌÍ¥¹…°ÕÍ•È¸¸¸ˆ)•á•Œ½ÍÔÍ¥¹…°€½…ÁÀ½Ù•¹Ø½‰¥¸½ÁåÑ¡½¸€µ´ÕÙ¥½É¸…ÁÀ¹µ…¥¸é…ÁÀ€´µ¡½ÍÐ€À¸À¸À¸À€´µÁ½ÉÐ€àÀÀÄ(