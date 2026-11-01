#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Antigravity Mobile Android APK Builder (Pure Android SDK Build-Tools + JDK 17)
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
DIST_DIR="$PROJECT_ROOT/dist"

JAVA_HOME="/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
JAVAC="$JAVA_HOME/bin/javac"
KEYTOOL="$JAVA_HOME/bin/keytool"

ANDROID_SDK="/opt/homebrew/share/android-commandlinetools"
BUILD_TOOLS="$ANDROID_SDK/build-tools/35.0.0"
PLATFORM_JAR="$ANDROID_SDK/platforms/android-34/android.jar"

AAPT2="$BUILD_TOOLS/aapt2"
D8="$BUILD_TOOLS/d8"
ZIPALIGN="$BUILD_TOOLS/zipalign"
APKSIGNER="$BUILD_TOOLS/apksigner"

export JAVA_HOME
export PATH="$JAVA_HOME/bin:$BUILD_TOOLS:$PATH"

echo "🔨 Preparing build directories..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/compiled_res" "$BUILD_DIR/res_src" "$BUILD_DIR/gen" "$BUILD_DIR/classes" "$BUILD_DIR/apk_root/assets" "$DIST_DIR"

# Load optional .env from project root for custom default server URL (unless AGY_CLEAN_BUILD=1)
if [ "${AGY_CLEAN_BUILD:-0}" != "1" ] && [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

# 1. Copy latest web UI bundle and Android resources into build staging
cp "$PROJECT_ROOT/server/web/index.html" "$BUILD_DIR/apk_root/assets/index.html"
cp -R "$SCRIPT_DIR/res/"* "$BUILD_DIR/res_src/"

if [ "${AGY_CLEAN_BUILD:-0}" = "1" ]; then
  rm -rf "$BUILD_DIR/res_src/raw"
elif [ -n "${AGY_VPS_PUBLIC_URL:-}" ]; then
  sed -i '' "s|http://127.0.0.1:8899|${AGY_VPS_PUBLIC_URL}|g" "$BUILD_DIR/res_src/values/strings.xml"
  sed -i '' "s|http://127.0.0.1:8899|${AGY_VPS_PUBLIC_URL}|g" "$BUILD_DIR/apk_root/assets/index.html"
  if [ -f "$BUILD_DIR/res_src/raw/server_cert.pem" ]; then
    sed -i '' 's|<certificates src="user" />|<certificates src="user" /><certificates src="@raw/server_cert" />|g' "$BUILD_DIR/res_src/xml/network_security_config.xml"
  fi
fi

# 2. Compile Android resources with aapt2
echo "📦 Compiling Android resources with aapt2..."
"$AAPT2" compile --dir "$BUILD_DIR/res_src" -o "$BUILD_DIR/compiled_res/resources.zip"

"$AAPT2" link \
  -I "$PLATFORM_JAR" \
  --manifest "$SCRIPT_DIR/AndroidManifest.xml" \
  --java "$BUILD_DIR/gen" \
  -A "$BUILD_DIR/apk_root/assets" \
  -o "$BUILD_DIR/unaligned.apk" \
  "$BUILD_DIR/compiled_res/resources.zip"

# 3. Compile Java sources against android-34
echo "☕ Compiling Java sources with OpenJDK 17..."
"$JAVAC" \
  -source 8 -target 8 \
  -bootclasspath "$PLATFORM_JAR" \
  -classpath "$PLATFORM_JAR" \
  -d "$BUILD_DIR/classes" \
  "$BUILD_DIR/gen/com/antigravity/mobile/R.java" \
  "$SCRIPT_DIR/src/com/antigravity/mobile/ApkProvider.java" \
  "$SCRIPT_DIR/src/com/antigravity/mobile/MainActivity.java"

# 4. Convert .class files to classes.dex using d8
echo "⚙️ Converting bytecode to DEX with d8..."
CLASS_FILES=()
while IFS= read -r -d '' f; do
  CLASS_FILES+=("$f")
done < <(find "$BUILD_DIR/classes" -name "*.class" -print0)

"$D8" --lib "$PLATFORM_JAR" --min-api 24 --output "$BUILD_DIR" "${CLASS_FILES[@]}"

# 5. Add classes.dex to the linked APK
echo "📥 Packaging classes.dex into APK..."
(cd "$BUILD_DIR" && zip -q -u "$BUILD_DIR/unaligned.apk" classes.dex)

# 6. Align APK with zipalign
echo "📐 Aligning APK with zipalign..."
"$ZIPALIGN" -f -p 4 "$BUILD_DIR/unaligned.apk" "$BUILD_DIR/aligned.apk"

# 7. Sign APK with apksigner
KEYSTORE="$SCRIPT_DIR/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
  echo "🔑 Generating Android signing keystore..."
  "$KEYTOOL" -genkeypair \
    -v \
    -keystore "$KEYSTORE" \
    -storepass android \
    -alias androiddebugkey \
    -keypass android \
    -keyalg RSA \
    -keysize 2048 \
    -validity 10000 \
    -dname "CN=Antigravity Mobile,O=Antigravity,C=US" >/dev/null 2>&1
fi

OUTPUT_APK="$DIST_DIR/AntigravityMobile.apk"
echo "✍️ Signing APK -> $OUTPUT_APK..."
"$APKSIGNER" sign \
  --ks "$KEYSTORE" \
  --ks-pass pass:android \
  --key-pass pass:android \
  --ks-key-alias androiddebugkey \
  --out "$OUTPUT_APK" \
  "$BUILD_DIR/aligned.apk"

"$APKSIGNER" verify "$OUTPUT_APK"
cp -f "$OUTPUT_APK" "$HOME/Desktop/AntigravityMobile.apk" 2>/dev/null || true
echo "✅ Successfully built signed APK: $OUTPUT_APK ($(du -h "$OUTPUT_APK" | cut -f1))"
