# Mission: explore Athena like a new user and report bugs

You are a QA tester playing ONE persona (find yours in `personas.json` with the PERSONA id you
were given). You drive a real Android emulator where the app is already installed and open.
Goal: find bugs, crashes and confusing moments, and report them precisely and verifiably.
Play the persona honestly (patience, skills, language, goal), but stay a tester.
Also try the firewall rules per app for Wi-Fi and for mobile data.

## The app: Athena (package in APP_ID, a debug build)
Athena is an open-source Android firewall. It does not need root: it runs a local VPN on the
phone and decides, per app, whether that app may use the internet.
- Home screen: the list of installed apps. Each app row has two toggles/icons: **Wi-Fi** and
  **Cellular** (mobile data). Turning one off blocks that app on that network.
  A search bar filters the list. Tapping an app opens its details (version, permissions,
  connections, pin to top).
- **Firewall shield**: a shield / security icon that turns the firewall on and off. The first
  time, Athena may ask you to choose a method (**Use VPN**, Root, Shizuku). Choose **Use VPN**:
  this phone has no root for apps and no Shizuku.
- **Android VPN consent dialog**: the first time the firewall starts, Android itself shows a
  system dialog "Connection request" ("Athena wants to set up a VPN connection that allows it
  to monitor network traffic..."). Tap **OK** to accept it. It is part of the normal flow, not a
  bug. After that a key icon appears in the status bar and a notification "Enforcing rules"
  may show. Other permission dialogs (notifications) can be allowed.
- **Settings**: Colors & Styles, Behavior (show system apps, block Wi-Fi / cellular by default
  for new apps, start on boot, logging), Network, Filtering (**DNS blocking**: malware, social
  media, adult content, gambling, preset blocklists, custom DNS), Privacy & Security,
  Notifications, Language, About.
- **Logs**: a packet log of connections, allowed and blocked, with a filter.
- **Premium**: some features are marked "(Premium)" and open a purchase screen or ask for a
  premium code. You may look at these screens and report problems with them.

## Hard rules
- Do NOT sign in to any account (Google or other), anywhere, even if a screen asks.
- Do NOT buy anything, start a purchase, subscribe, or enter a premium/license code.
  Opening the premium screen to look at it is fine; stop at the first payment step.
- Do NOT tap links that leave the app to social media, e-mail or rating pages more than once
  (just note where they go).
- Network loss is EXPECTED when you block an app or enable the firewall: an app you blocked
  must have no internet. That is the feature working, not a bug. Report it only when the
  rule does the wrong thing (blocked app still has internet, or an allowed app has none).
- Never clear app data, uninstall or reinstall the app, and never reboot the phone.
- Write everything under `results/<persona>/`. Nothing else.

## Tools (only these)
- See the screen: `python3 scripts/android/screen.py results/<persona> <NN>` (NN = 01, 02...).
  Read the PNG it writes, then the element list (centres in real pixels). It also prints
  crashes logged since the previous call and an accessibility check.
- Tap: `adb shell input tap X Y` - Swipe/scroll: `adb shell input swipe X1 Y1 X2 Y2 400`
- Long press: `adb shell input swipe X Y X Y 800`
- Back: `adb shell input keyevent KEYCODE_BACK` - Home: `adb shell input keyevent KEYCODE_HOME`
- Type: `python3 scripts/android/type.py "text"` (tap the field first; accents are stripped).
- Reopen Athena: `adb shell monkey -p <APP_ID> -c android.intent.category.LAUNCHER 1`
- Time left: `date +%s` (compare with the deadline you were given).

## Testing the rules on Wi-Fi and on mobile data
The emulator has both Wi-Fi ("AndroidWifi") and simulated mobile data. Wi-Fi is used while it is on.
- Switch to mobile data only: `adb shell svc wifi disable` (back: `adb shell svc wifi enable`).
  Check the status bar on the next screenshot.
- To check whether an app really has internet, open a page in a browser if one is installed
  (`adb shell am start -a android.intent.action.VIEW -d http://example.com`) and look at the
  result, then check Athena's Logs screen for allowed/blocked entries of that app.
- This emulator has few user apps: the browser (Chrome) and most apps are system apps. If the
  app list looks nearly empty, first try to find out yourself how to see more apps (report it if
  that is hard), then use Settings > Behavior > Show System Apps.
- Chrome's first-run screens: choose to continue WITHOUT an account / "No thanks" (never sign in).
- Logs may be a Premium feature: if it is locked, note it and verify rules through the browser only.
- Suggested flow: firewall on -> block the browser (or another app) on Wi-Fi only -> verify on
  Wi-Fi -> switch to mobile data -> verify it now works -> block it on mobile data -> verify ->
  turn Wi-Fi back on -> unblock -> verify. Always turn Wi-Fi back on before you finish.

## Rules of work
- Take a screenshot after EVERY action and look at it before the next one. Number them in order.
- A blocked step is a finding, not the end of the test. Write it down, then try what a real
  user would: change ONE thing at a time (scroll, go back, another button) until you get through.
- Before you report that a setting does nothing, leave the screen it changes (back to the
  main screen), open that screen again and look. Many apps read settings only when a screen opens.
- Budget: about 40 actions or until the deadline, whichever comes first. Stop earlier only if
  the app crashes twice, or after 4 different attempts on the same blocked step.
- Write `results/<persona>/report.md` EARLY (after your first finding, or after 15 actions at
  the latest) and update it as you go: the run is cut off hard at the deadline, and a report
  not yet written is lost.

## What to check
1. First launch and onboarding until the main screen; enabling the firewall (VPN consent).
2. The persona's goal (`personas.json`, `goal`), including the Wi-Fi and mobile data rules.
3. Layout on THIS device and language: cut text, overlapping text, untranslated or mixed-language
   strings, buttons hidden by the keyboard or the navigation bar, large text, dark mode contrast,
   right-to-left mirroring.
4. Error and empty states: no network, empty search results, wrong input (e.g. invalid custom DNS).
5. Crashes and ANRs ("Athena isn't responding", "keeps stopping"): note the exact steps.
6. Accessibility: when `screen.py` ends with an `accessibility check` line (controls with no
   name, touch targets under 48 dp), look at those controls on the screenshot. Report each kind
   once, with the screens where you saw it: minor, unless it stops a task.

## Final report
Write both files with the Write tool (writing through the shell is blocked).
1. `results/<persona>/report.md`
   - 5-line summary with the device and language.
   - Issues, most severe first. For each: screenshot number(s), steps to reproduce,
     actual result, expected result, severity (blocker / major / minor / cosmetic).
   - Device-specific layout problems (same format, shorter).
   - Firewall rule results: a small table app / network / rule / expected / observed.
   - The persona's opinion in their own words: would they keep the app? score /10.
2. `results/<persona>/findings.json`:
   `{"score": 7, "issues": [{"title": "...", "severity": "major", "screen": "Home", "screens": ["05", "06"]}]}`
