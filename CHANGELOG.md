# Changelog

## 1.1.5 - Production Readiness

- Refined the room lifecycle around Home, Cast, Music, and Settings with browser-first listener links.
- Improved synchronized playback with `playAt` scheduling, shared LAN/WAN listener assets, and LAN binary PCM negotiation.
- Hardened WAN relay behavior, listener cleanup, release workflows, and F-Droid unsigned artifact generation.
- Updated app/server version metadata to `1.1.5` with Android `versionCode` `5`.

## 1.1.4 - Release and Listener Polish

- Fixed GitHub Release permissions and APK artifact paths.
- Improved browser listener buffering, retry cleanup, and late-listener metadata.
- Separated Room PINs from Server Connection PINs for protected WAN deployments.
- Shortened public documentation and release notes.

## 1.1.0 - Broadcast Control Refresh

- Added explicit LAN, Internet, and LAN + Internet room selection.
- Added active broadcast persistence with return and stop actions.
- Improved WAN browser listener compatibility and server PIN relay startup.
- Bumped app and server metadata to `1.1.0`.
