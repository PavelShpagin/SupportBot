#!/bin/bash
# Fix signal-cli for ARM64 by downloading the native library
set -e

echo "=== Fixing signal-cli for ARM64 ==="

# Find libsignal-client version
LIBSIGNAL_JAR=$(docker exec supportbot-api ls /opt/signal-cli-0.13.24/lib/ | grep "libsignal-client" | head -1)
echo "Found: $LIBSIGNAL_JAR"

# Extract version number (e.g., libsignal-client-0.64.1.jar -> 0.64.1)
VERSION=$(echo "$LIBSIGNAL_JAR" | sed 's/libsignal-client-\(.*\)\.jar/\1/')
echo "libsignal-client version: $VERSION"

# Download ARM64 native library from community builds
# Using exquo/signal-libs-build releases
echo "Downloading ARM64 native library..."

# First, find the SO version number needed
SO_VERSION=$(docker exec supportbot-api unzip -l /opt/signal-cli-0.13.24/lib/$LIBSIGNAL_JAR 2>/dev/null | grep "libsignal_jni" | awk '{print $4}' | head -1)
echo "Looking for native lib pattern: $SO_VERSION"

# Download from projektzentrisch (has more versions)
# The file naming is libsignal_jni_soXXXX_arm64.gz where XXXX is the SO version
# For libsignal-client 0.64.x, the SO version is typically around 6401

# Let's check what's inside the jar
echo "Contents of libsignal jar:"
docker exec supportbot-api unzip -l /opt/signal-cli-0.13.24/lib/$LIBSIGNAL_JAR | grep -E "\.so|\.dylib|\.dll" || echo "No native libs found in jar"

# Download the ARM64 library
DOWNLOAD_URL="https://github.com/AsamK/signal-cli/releases/download/v0.13.24/libsignal_jni_aarch64.so.gz"
echo "Trying official release first: $DOWNLOAD_URL"

cd /tmp
curl -fsSL "$DOWNLOAD_URL" -o libsignal_jni_aarch64.so.gz 2>/dev/null && {
    gunzip -f libsignal_jni_aarch64.so.gz
    echo "Downloaded from official release"
} || {
    echo "Official release doesn't have ARM64, trying community build..."
    # Try exquo builds
    curl -fsSL "https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${VERSION}/libsignal_jni_aarch64-unknown-linux-gnu.so.tar.gz" | tar -xzf -
    mv libsignal_jni.so libsignal_jni_aarch64.so
}

# Copy into the container
echo "Copying native library to container..."
docker cp /tmp/libsignal_jni_aarch64.so supportbot-api:/tmp/

# Add to the JAR file
echo "Adding to JAR file..."
docker exec supportbot-api bash -c "cd /tmp && zip /opt/signal-cli-0.13.24/lib/$LIBSIGNAL_JAR libsignal_jni_aarch64.so"

# Alternatively, put it in java.library.path
echo "Also copying to java library path..."
docker exec supportbot-api bash -c "cp /tmp/libsignal_jni_aarch64.so /usr/lib/libsignal_jni.so"

echo "=== Testing signal-cli ==="
docker exec supportbot-api signal-cli --version || echo "Still failing, may need container restart"

echo "=== Done! ==="
echo "If still failing, try: docker restart supportbot-api"
