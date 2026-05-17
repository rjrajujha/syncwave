# GitHub Actions workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yml` | push / PR | Flutter + Python CI |
| `flutter-release.yml` | tag `v*` / manual | GitHub Release APK |
| `fdroid-release.yml` | tag `v*` / manual | Unsigned APK artifact + Fastlane metadata |

## Android signing (`flutter-release.yml`)

Secrets must be **GitHub Actions repository secrets**:

**Settings → Secrets and variables → Actions → Repository secrets**

Do **not** use **Agents secrets** (`/settings/secrets/agents`) — workflows cannot read those.

| Secret | Description |
|--------|-------------|
| `ANDROID_KEYSTORE_BASE64` | Base64 of `.jks` file (no line breaks) |
| `ANDROID_KEYSTORE_PASSWORD` | Keystore password |
| `ANDROID_KEY_ALIAS` | Key alias |
| `ANDROID_KEY_PASSWORD` | Key password |

Generate base64 locally:

```sh
base64 -i your-release.keystore | tr -d '\n' | pbcopy   # macOS
```

**Behavior:**

- All four Actions secrets set → signed APK (`app-release-signed.apk` + `app-release.apk`)
- None set → unsigned APK with workflow warning
- Partial set → fail with explicit missing names

Local signing: `apps/android/key.properties.example` → `key.properties` + keystore (gitignored).

## F-Droid (`fdroid-release.yml`)

No secrets. Builds with `-Psyncwave.fdroid=true` (no release signing). Uploads APK + `fastlane/metadata/android/en-US/`.

## Artifacts

| Workflow | Output |
|----------|--------|
| GitHub Release | `release-artifacts/app-release.apk` |
| F-Droid CI | `syncwave-fdroid-<tag>` artifact |
