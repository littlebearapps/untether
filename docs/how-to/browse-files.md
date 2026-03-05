# Browse project files

Browse your project's directory tree and preview files without leaving [Telegram](https://telegram.org) — check a config, review a file, or orient yourself in the repo from your phone or any device.

## Start browsing

Send `/browse` to open the project root:

```
/browse
```

Untether replies with a directory listing rendered as inline keyboard buttons. Each button is a file or directory you can tap.

<!-- SCREENSHOT: /browse showing project root with directory and file buttons -->

## Navigate directories

Tap a directory button to drill into it. The listing updates in place, showing the contents of the selected directory.

## Preview a file

Tap a file button to see a syntax-highlighted preview. Previews show up to **25 lines** and **2,000 characters** of the file content, which is enough to check config files, review small modules, or confirm file structure.

!!! untether "Untether"
    **src/main.py**
    ```python
    import sys
    from pathlib import Path

    from untether.app import create_app

    def main():
        app = create_app()
        app.run()
    ```

## Go back

The `(..)` button at the top of every listing navigates to the parent directory. Tap it to move up one level.

## Browse a specific path

Pass a path argument to jump directly to a directory or file:

```
/browse src/
/browse package.json
```

If the path is a directory, Untether shows its listing. If it's a file, you get the preview directly.

## Limits and filtering

The file browser applies sensible defaults to keep listings readable:

| Limit | Value |
|-------|-------|
| Max entries per listing | 20 |
| Hidden files | Skipped (except `.env.example`) |
| Excluded directories | `__pycache__`, `node_modules`, `.git`, `.venv` |

If a directory has more than 20 entries, only the first 20 are shown. Use `/browse path/to/subdir` to navigate deeper.

## Path traversal protection

The browser cannot navigate outside the project root. Any attempt to use `..` to escape the project directory is blocked — you can only browse files within the configured project path.

## Related

- [Projects](projects.md) — register repos and set project roots
- [Commands & directives](../reference/commands-and-directives.md) — full command reference
