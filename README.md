# Stavrobot Books plugin

A planned [Stavrobot](https://github.com/skorokithakis/stavrobot) plugin for requesting ebooks and audiobooks through a self-hosted media stack and checking when they are available to read or listen to.

The intended integration uses:

- [Bookshelf](https://github.com/pennydreadful/bookshelf) for discovery and acquisition.
- [Calibre-Web Automated](https://github.com/crocodilestick/Calibre-Web-Automated) as the final ebook library.
- [Audiobookshelf](https://github.com/advplyr/audiobookshelf) as the final audiobook library.
- Self-hosted Hardcover-compatible metadata behind Bookshelf.

## Status

This repository currently contains the project scaffold only. Tool manifests, runtime code, configuration documentation, and tests still need to be implemented.

The target conversational workflow is:

1. Search for a book and return unambiguous candidates.
2. Request the selected candidate as an ebook or audiobook.
3. Track acquisition, import, and library handoff as one request.
4. Confirm completion only after the title is visible in the destination library.

The plugin will be designed around explicit candidate selection, stable identifiers, bounded read operations, and a deliberately small mutation surface. Local credentials belong in an untracked `config.json` and must never be committed.
