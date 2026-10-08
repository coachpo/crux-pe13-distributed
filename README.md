# Distributed Crux PixelExperience 13 build experiment

This repository owns an experiment to compile the Crux PE13 ROM on multiple
standard public GitHub Actions Ubuntu x64 runners, then assemble its image
outputs. It does not change the maintained device, platform, kernel or U-Boot
repositories and does not operate a phone.

The build inputs use the published October 8, 2026 Crux source selection. The
existing generated Android Ninja graph is a planning input: compilation is
performed from source in cold runner tasks. An existing installed ROM binary is
never a substitute for a successful distributed build.

Implementation and actual result receipts will be added as the experiment runs.
The experiment is not yet a completed ROM build.
