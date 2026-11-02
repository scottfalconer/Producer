# Developing Producer

Keep the presenter experience quiet and hands-free. Editorial judgment belongs primarily in the director's prompt and model tools; deterministic validation, routing, state/lifecycle and permission enforcement belong in local code.

## Local setup

Follow [SETUP](SETUP.md) for Python dependencies. A live model session is not needed to run the fixture-based unit and browser checks.

From the repository root with the virtual environment active:

```sh
PYTHONPATH=producer python -m unittest discover -s producer -p 'test_*.py' -v
```

The tests use temporary files and mocked provider/Twitch transports. FFmpeg is needed for media ingestion tests; loopback HTTP/socket access is needed for integration tests. Report skipped tests and environmental failures rather than counting them as passes. Never use a real credential to make a fixture test pass.

Optional browser checks and replay rendering:

```sh
npm --prefix producer ci
cd producer
npx playwright install chrome
cd ..
node producer/native-obs-browser-check.cjs
node producer/realtime-browser-check.cjs
```

`package-lock.json` pins the development dependency tree. These checks select Playwright's `chrome` channel, so an installed Google Chrome is required (skip the install command if already installed). Use a Node version supported by the pinned Playwright release. These browser checks exercise local fixture servers; they do not prove actual model or Twitch behavior. A complete recorded-show run is a separate, explicitly invoked command that uses the supplied API key and incurs inference charges.

## Make a change

1. Reproduce the issue at the right layer: input, model judgment, tool validation, routing or rendering.
2. Keep changes scoped. Preserve source/session identity, offline defaults, private/public separation, Stop cleanup and actual delivery receipts.
3. Add a regression for a concrete bug. Test boundary behavior and observable outcomes, not a duplicate of the implementation.
4. Run relevant checks and record the exact command/result. Broad tests are appropriate for shared routing or lifecycle changes.
5. Update the setup or architecture docs when public behavior changes.

For UI changes, use the shared components/tokens and inspect presenter/audience views separately. Do not add operator controls to the passive confidence monitor. For prompt changes, use a fresh recorded-show inference run; replaying old actions cannot establish improved model judgment.

## Private files and release content

Keep keys, OAuth tokens, recordings, transcripts, show notes, research packets and captured debug bundles in ignored local paths. `.local/` is the project's working-data area. Do not copy personal show folders, browser state, downloaded reference archives or development conversation exports into a contribution.

Use synthetic inputs for reproducible tests and preserve their disclosure. Generated fixture author names are not actual viewers. Third-party assets need their original license/attribution; the MIT license for Producer does not relicense dependencies or your media.

Before publishing, inspect the exact staged file list and run a secret scan over the export, not just the working tree. Review logs/screenshots separately; they can contain show content even if they contain no credentials. Do not push a copied working directory wholesale.
