# v1.0 launch QA

The release gate for v1.0 (issue #583). It is one pass on each platform, on production-like builds: the deployed web client, the Play build and the TestFlight build. Run it after every other v1.0 issue is closed. Unit tests and CI cover the code. This covers the parts that only fail on a real device or browser.

Trip ZIP import and the GPX share sheet on web and Android are checked separately, in #559.

## How to fill it in

- Record the builds under test in **Builds** before starting. A result only counts for the build it was run on. If a build changes mid-pass, rerun the rows it touches.
- In each cell write one of:
  - `✅`: passed.
  - `❌ #N`: failed, with the issue for that failure. Open it in the v1.0 milestone.
  - `n/a`: the platform does not have the feature. These are filled in already.
- Add short notes under the table, keyed by row id (e.g. `B3`).
- Commit the filled-in file. Its history is the record of the pass.

**Done when** every cell is `✅` or `n/a`, and every `❌` has its own v1.0 issue that has been fixed and rechecked.

## Builds

| Platform | Build | Device / browser | Date | Tester |
|---|---|---|---|---|
| Web (Chrome) | | | | |
| Web (Safari or Firefox) | | | | |
| Android (Play) | | | | |
| iOS (TestFlight) | | | | |

- **Play build:** install it from the internal track as a Play internal tester ([ANDROID.md](ANDROID.md)), not by sideloading the APK.
- **TestFlight build:** install it from the TestFlight app as an internal tester, after answering the build's export-compliance question ([IOS.md](IOS.md)).

## Accounts

| Id | Check | Web Chrome | Web Safari/Firefox | Android | iOS |
|---|---|---|---|---|---|
| A1 | Sign up with e-mail, verify the e-mail, log out, log back in | | | | |
| A2 | Google Sign-In: a new account | | | | |
| A3 | Google Sign-In: a returning account | | | | |
| A4 | Sign in with Apple, including "Hide my e-mail" (required on iOS; elsewhere only if offered) | | | | |
| A5 | Password reset | | | | |
| A6 | Delete the account from inside the app (Apple and Google both require it) | | | | |

## Billing

| Id | Check | Web Chrome | Web Safari/Firefox | Android | iOS |
|---|---|---|---|---|---|
| B1 | Stripe checkout | | | n/a | n/a |
| B2 | Plan change | | | | |
| B3 | Cancellation (from the store on mobile) | | | | |
| B4 | Withdrawal (14-day pro-rata refund) | | | n/a | n/a |
| B5 | In-app purchase | n/a | n/a | | |
| B6 | Restore purchases | n/a | n/a | | |
| B7 | A web subscriber opening the mobile app sees their plan and is not offered a second purchase | n/a | n/a | | |
| B8 | Free-plan limits show and are enforced | | | | |

## End-to-end encryption

E2EE was checked byte-for-byte on web and the Dart VM. There is no record of a check on a real Android or iOS device.

| Id | Check | Web Chrome | Web Safari/Firefox | Android | iOS |
|---|---|---|---|---|---|
| E1 | Enable encryption, create an encrypted memory and journal entry, read them back | | | | |
| E2 | Approve a second device and read the same content there, across platforms (for example web → iOS) | | | | |
| E3 | Recover with the recovery key on a fresh install | | | | |
| E4 | Share an encrypted memory and open the share link logged out | | | | |
| E5 | Encrypted account + travel companions (blocked in v1, #108): the app explains why instead of showing a raw 409 | | | | |

For E2, record which pair of platforms was used in the notes.

## Core flows

| Id | Check | Web Chrome | Web Safari/Firefox | Android | iOS |
|---|---|---|---|---|---|
| C1 | Create a trip | | | | |
| C2 | Import activities: Strava | | | | |
| C3 | Import activities: GPX | | | | |
| C4 | Import activities: Polarsteps | | | | |
| C5 | Edit a trip, view the map | | | | |
| C6 | Export a trip | | | | |
| C7 | Share and invite links open the app (App Links / Universal Links) | n/a | n/a | | |
| C8 | Offline: open a saved trip with no network | | | | |
| C9 | "Open in TraxJourney" for a `.gpx` from Mail and Files (declared in `Info.plist`, never verified; moved from #559) | n/a | n/a | n/a | |

## Notes

<!-- One line per note: `A4 (iOS): ...` -->
