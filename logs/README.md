# Fishing Mod Diagnostic Logs

These logs are used only for troubleshooting. Depending on the selected logging mode, they may contain:

- event timestamps and temporary process/thread IDs;
- Python, pyMHF, NMSpy, SDL, Vulkan, and GPU driver versions;
- GPU model, vendor, type, and model-level `vendor/device ID`;
- controller type and model-level `vendor/product ID`;
- window resolution, Vulkan/swapchain parameters, and overlay state;
- selected presets, debug flags, and mod setting changes;
- fish, class, size, difficulty, minigame state, and result;
- only the gameplay inputs used by the mod: SPACE, the controller action button, configured F-keys, and diagnostic mouse buttons;
- pipe messages, errors, and tracebacks. User-specific paths are redacted (`<USERPROFILE>`, `<GAME_DIR>`, `<MOD_DIR>`, and similar placeholders);
- in debug/trace modes, temporary memory addresses and more detailed gameplay state.

The mod does not collect Steam IDs, user or computer names, IP/MAC addresses, serial numbers, device UUIDs/LUIDs, process lists, file contents, or arbitrary typed text.

Logs are never uploaded or sent automatically. They remain on your computer. You decide whether to share them with the developer for troubleshooting.
