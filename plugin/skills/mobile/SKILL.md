---
name: mobile
description: Mobile app checklists for iOS (Swift, SwiftUI, UIKit) and Android (Kotlin, Jetpack Compose)—platform conventions, state and offline behavior, permissions and privacy declarations, security, tests on the smallest and largest devices, signing and store-ready builds. Use when building, changing, reviewing or releasing an iOS or Android app, when editing its manifest, Info.plist, entitlements or privacy manifest, or when a mobile build or signing step fails.
---

# Mobile methodology

Apply what fits the change; follow the project's existing architecture, libraries, and minimum OS versions. Read the
versions before using an API: the deployment target in the Xcode project or `Package.swift`, and `minSdk`,
`targetSdk` and `compileSdk` in the module's Gradle file and `gradle/libs.versions.toml`. Guard a newer API with
`#available` or a `Build.VERSION.SDK_INT` check instead of raising the minimum.

## Platform skills

| Platform | Load | When |
| --- | --- | --- |
| iOS | `swiftui-pro` | Writing or reviewing SwiftUI views (the `swiftui` rule requires it) |
| Android | `android-permissions-security` | The manifest, exported components, IPC, and runtime permissions |
| Android | `jetpack-compose-performance` | Jank, recomposition, lazy lists, a slow screen |
| Both | `impeccable` | The design and critique of a screen |

## Platform conventions

- Follow the platform, not the other one: Apple's Human Interface Guidelines on iOS, Material 3 on Android; system
  back navigation, system sheets and pickers, the platform's date and number formats.
- Respect the user's settings: Dynamic Type and font scale, dark mode, Reduce Motion, right-to-left languages.
- Lay out inside the safe areas and system bar insets (edge to edge on Android); never assume a screen size.
- Accessibility: a label for every control that has no visible text (VoiceOver, TalkBack), touch targets of at least
  44 × 44 pt on iOS and 48 × 48 dp on Android, and no meaning carried by color alone.
- Every user-facing string comes from the localization files (String Catalogs, `strings.xml`), never from code.

## State, data, and offline

- The user's place survives rotation, backgrounding and process death: restore the screen, the scroll position, and
  unsaved input.
- No network, disk or heavy work on the main thread; show progress for anything slower than a moment.
- Assume a slow or absent network: timeouts on every request, bounded retries with backoff only for idempotent
  calls, cached data shown with its age, and writes made offline queued and synced later (WorkManager on Android,
  background tasks on iOS).
- Handle every state of a screen: loading, empty, error, partial, offline, and signed out.

## Permissions and privacy

- Ask for a permission when the feature needs it, after saying why, and keep the app usable when the answer is no;
  after a permanent denial, offer the path to Settings instead of asking again.
- Prefer a flow that needs no permission (the system photo picker, approximate location) when it is enough.
- iOS: every protected resource the app uses has its usage description in `Info.plist` (`NSCameraUsageDescription`
  and the rest); the privacy manifest (`PrivacyInfo.xcprivacy`) records the data the app collects and the required
  reasons APIs it uses, and each third-party SDK brings its own.
- Android: declare permissions in the manifest, request dangerous ones at runtime, and set `android:exported`
  explicitly on every component; keep private components unexported.
- The store declarations match the code: the App Store privacy details, and Google Play's Data safety form, which
  covers what third-party SDKs collect too. A new SDK or a new kind of data means updating both.

## Security

- The app binary is public: no secrets in it, in `Info.plist`, in `BuildConfig`, or in resources. Restrict API keys
  on the server side.
- Tokens and credentials go to the Keychain on iOS and to Keystore-backed storage on Android, never to
  `UserDefaults` or plain shared preferences.
- HTTPS only: keep App Transport Security and Android's network security configuration strict; no blanket
  exceptions.
- Validate everything that arrives from outside: deep links, intents, push payloads, pasted text. Verify Universal
  Links and App Links.
- Logs and crash reports carry no personal data or tokens.

## Testing

| Layer | iOS | Android |
| --- | --- | --- |
| Unit | Swift Testing (`@Test`) or XCTest | JUnit with fakes; Robolectric where Android classes are needed |
| UI | XCUITest | Compose UI tests (`createComposeRule`), Espresso for Views |
| Screenshot | The project's tool, if it has one | Compose Preview Screenshot Testing, Roborazzi or Paparazzi, as the project uses |

- Run the narrowest target first: `xcodebuild test -scheme <scheme> -destination 'platform=iOS Simulator,name=<device>'
  -only-testing:<target>/<test>`, `./gradlew testDebugUnitTest --tests '<class>'`, then
  `./gradlew connectedDebugAndroidTest` on an emulator.
- Check each changed flow on the smallest and the largest supported device (a small phone, and a tablet or foldable),
  with the largest text size, in dark mode, with each permission denied, offline, and on a slow network.
- A bug fix starts with a failing test, as everywhere (the `testing` skill).

## Builds, signing, and release

- Signing keys, keystores, `.p12` files, provisioning profiles and their passwords never enter the repository; CI
  reads them from its secret store. On Google Play, keep the upload key apart from the app signing key that Play App
  Signing holds.
- Raise the build number on every upload (`CFBundleVersion`, `versionCode`) and the marketing version by the
  project's policy (`CFBundleShortVersionString`, `versionName`).
- Meet the stores' current SDK requirements before a release: Google Play's target API level rises every year, and
  App Store Connect requires a recent Xcode SDK. Read the requirement on the store's page; do not trust a remembered
  number.
- Test the release build, not only the debug one: R8 shrinking on Android and optimization on iOS can break
  reflection, serialization, and deep links. Keep the R8 mapping files and the dSYMs so crashes can be symbolicated.
- Commit the dependency locks (`Package.resolved`, the Gradle version catalog) and update them on purpose.

## Cross-platform apps

React Native, Flutter and Kotlin Multiplatform apps ship two native shells: apply the iOS and Android sections to
each shell (its manifest, `Info.plist`, privacy declarations and signing), and the testing table to each platform.

See also the `testing` skill for test strategy, `devsecops` for threat modeling and scans, and `release-policy` for
versioning and releases.
