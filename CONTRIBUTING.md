# Contributing to Nook

Small, focused fixes are welcome. Describe the concrete problem, the resulting behavior, and how you checked it. For substantial changes, open an issue first so the scope can be discussed.

Use your own bot and a separate Linux user or test server. Never include real tokens, provider sessions, private messages, or server inventory in commits, issues, or fixtures. Review the diff and the staged file list before pushing.

Run these checks from the source directory:

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_release.py
bash install.sh --check
```

Stage any new source files before building; the release builder uses Git's file list. Commit the regenerated `MANIFEST.json` with the source change. Do not commit `dist/` or runtime files. Add tests when they verify a meaningful behavior or regression.

English and Russian documentation improvements are welcome. The current Telegram interface is Russian; internationalization should preserve callback identifiers, owner checks, and confirmation semantics.

Contributions are provided under the project's MIT license. Do not include third-party code without its required notices and compatible permission.
