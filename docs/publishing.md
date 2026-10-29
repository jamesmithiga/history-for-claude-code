# Publishing "History for Claude Code"

The release file is ready at `vscode-extension\dist\history-for-claude-code.vsix` (id `jamesmithiga.history-for-claude-code`, version 0.1.0, MIT).

The account and token steps below are yours to do: they sign you up for services and handle your credentials.

## 1. Visual Studio Marketplace (VS Code)

1. Go to https://marketplace.visualstudio.com/manage and sign in with your Microsoft account.
2. **Create publisher**:
   - **ID:** `jamesmithiga`. It must match `publisher` in `package.json` exactly, and can't be changed later.
   - **Name:** James Mithiga, or whatever you want shown on the listing.
3. On the publisher page, choose **New extension → Visual Studio Code**, then upload `vscode-extension\dist\history-for-claude-code.vsix`.
   - This needs no token or command-line login.
   - The Marketplace scans the upload for a few minutes before the listing goes live.
4. Check the listing at https://marketplace.visualstudio.com/items?itemName=jamesmithiga.history-for-claude-code.

**Later releases:**
1. Bump `version` in `vscode-extension\package.json`.
2. Add an entry to `CHANGELOG.md`.
3. Run `npm run package` in `vscode-extension`.
4. On the publisher page, use **… → Update** on the extension and upload the new `.vsix`.

**Command-line publishing (optional, later):** `vsce publish` needs authentication. Azure DevOps personal access tokens stop working on **1 December 2026**. The recommended replacement is GitHub Actions *Trusted Publishing*, which needs the code in a GitHub repository. Until that's set up, uploading through the web page is the simplest route.

## 2. Open VSX (Cursor, Antigravity, VSCodium, Windsurf…)

1. Create an Eclipse Foundation account at https://accounts.eclipse.org. Enter your GitHub username in the profile.
2. Sign in at https://open-vsx.org with GitHub, open your profile, choose **Log in with Eclipse**, and sign the **Publisher Agreement**.
3. Under **Settings → Access Tokens**, generate a token. Copy it, because it's shown only once.
4. In **your own** terminal, from the repository's `vscode-extension` folder, run the two commands below, pasting your token where it says `<token>`:
   ```text
   npx ovsx create-namespace jamesmithiga -p <token>
   npx ovsx publish dist/history-for-claude-code.vsix -p <token>
   ```
5. Optionally, claim verified ownership of the `jamesmithiga` namespace (see the Open VSX wiki).

## 3. After publishing
- The old development build (`local.claude-history`) has already been uninstalled, and the release build (`jamesmithiga.history-for-claude-code` 0.1.0) is installed from the `.vsix`. Once the listing is live, VS Code treats the Marketplace copy as the same extension and updates it automatically.
- Your own `claude-history` MCP entry in `~/.claude.json` points at the `cch.py` in your clone of this repository, and the extension leaves it alone. Only registrations created by the extension itself are re-pointed after updates.

## Pre-publish checklist (done for 0.1.0)
- [x] Unique name and display name, with an "unofficial / not affiliated with Anthropic" notice
- [x] MIT `LICENSE`, `CHANGELOG.md`, PNG icon (256×256), public README with no relative links or SVGs
- [x] Package audited: 25 files with no personal data, history or index. Your name appears only in the license and publisher fields.
- [x] Tests: 30 Python, 4 Node unit, and 9 in-VS Code integration checks passing
- [ ] Optional: public GitHub repository, then add `repository`, `bugs` and `homepage` to `package.json` (needed for Trusted Publishing and gives users an issue tracker)
