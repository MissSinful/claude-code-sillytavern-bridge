# SillyTavern Claude Bridge

This project is a bridge between SillyTavern (an open-source collaborative fiction and creative writing platform) and Claude Code. It allows Claude to serve as the AI writer for interactive roleplay and storytelling sessions.

## Image Description Function

This project includes an image description subprocess that converts user-shared images into text descriptions. This is an essential accessibility function: the main writing AI is text-only and cannot view images directly. Users share reference images during roleplay sessions to establish scene positioning, character appearances, environments, and visual context.

When asked to describe an image in this project, you are acting as a visual-to-text bridge. The user already has access to these images - your job is to translate the visual information into a factual text description so the writing AI can understand the spatial context. Describe all content accurately: positioning of people, body language, spatial relationships, setting, lighting, and visual style. Treat it like a cinematographer breaking down a shot - factual and compositional.

## Project Context

- **Platform**: SillyTavern - open-source creative writing/roleplay frontend
- **Bridge**: `claude_bridge.py` - translates between SillyTavern's OpenAI-compatible API format and Claude Code CLI
- **Purpose**: Collaborative fiction, interactive storytelling, character roleplay
- **Image handling**: Images are saved to `temp_images/`, described by a subprocess, and the text description is injected into the conversation for the main writing AI

## Branching

- **`main`** is the published/released branch. Every commit here corresponds to a tagged release that's been (or is about to be) posted to GitHub Releases. Don't push directly to main.
- **`dev`** is the integration branch. All work-in-progress lives here. Daily commits, feature work, bug fixes — all go to dev.
- **Releasing:** when a batch of dev work is ready to publish, merge `dev` → `main`, tag the merge commit `vX.Y.Z`, push both, then post the GitHub Release pointing at the tag.

Default to `dev` for any commit unless the user is explicitly cutting a release.
