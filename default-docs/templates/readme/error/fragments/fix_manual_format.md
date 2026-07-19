<!-- block:zh -->
## 解决方法

> [!TIP]
> 未能从当前仓库名中识别出年份，无法自动推断正确名称，请手动确认后改名。

1. **核对信息**：在 [TMDB](https://www.themoviedb.org/) 搜索确认正确的英文片名与年份。
2. **重命名**：前往 **[Settings](../../settings)** --> **Repository name**，将仓库名改为 `英文片名_年份` 格式（如 `Sintel_2010`）并保存。
3. **重新运行**：回到 **[Actions](../../actions/workflows/init.yml)** 页面，选择 **"初始化 (Initialize)"** 工作流，点击 **Run workflow** 重新运行。
<!-- /block:zh -->

---

<!-- block:en -->
## How to Fix

> [!TIP]
> No year could be recognized in the current repository name, so a correct name cannot be inferred automatically — please rename manually after verifying.

1. **Verify Info**: Search [TMDB](https://www.themoviedb.org/) to confirm the correct English title and year.
2. **Rename**: Go to **[Settings](../../settings)** --> **Repository name**, rename it to the `EnglishTitle_Year` format (e.g. `Sintel_2010`) and save.
3. **Re-run**: Go back to **[Actions](../../actions/workflows/init.yml)**, select the **"Initialize"** workflow, and click **Run workflow** again.
<!-- /block:en -->
