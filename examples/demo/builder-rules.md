# Rules for the builders of Shelfmark

An invented example: the demo's config names this file in `rules.files`, and the web view shows it on its Rules tab.

1. **Read the brief first.** Its last section, the owner's answers, beats the rest of it.
2. **One car, one branch.** Start from the main branch; the branch is named after the car (`tr-0001-1`).
3. **Leave your files in the run's folder.** `agents start` prints it (`the run's files: …`): screenshots, logs and
   timings go there, and nowhere else.
4. **Tests before the pull request.** `python3 -m unittest`, all green; the pull request says which ones ran.
5. **No real data.** Every title, author and reader in a test is made up.
