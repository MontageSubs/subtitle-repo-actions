<!-- block:zh -->
## 解决方法

> [!TIP]
> TMDB 未能匹配到影片，请根据实际情况选择处理方案：

**方案 A：修正名称（适用于拼写错误）**

1. **核对信息**：在 [TMDB](https://www.themoviedb.org/) 搜索确认正确的英文片名与年份。
2. **重命名**：前往 **[Settings](../../settings)** --> **Repository name**，将仓库名修改为正确的 `英文片名_年份` 格式。
3. **重新运行**：回到 **[Actions](../../actions/workflows/init.yml)** 页面重新运行工作流。

**方案 B：强制初始化（适用于 TMDB 未收录）**

1. **触发运行**：前往 **[Actions](../../actions/workflows/init.yml)** 页面，选择 **"初始化 (Initialize)"** 工作流。
2. **启用强制模式**：点击 **Run workflow** 前，勾选 **“强制初始化 / Force init”** 选项。
3. **手动补全**：此操作将生成空白模板（信息仅基于仓库名），请在运行后自行核实并手动补全 README 内容。
<!-- /block:zh -->


---

<!-- block:en -->
## How to Fix

> [!TIP]
> TMDB could not find a matching title. Please choose a solution based on your situation:

**Option A: Correct Name (for spelling or year errors)**

1. **Verify Info**: Search [TMDB](https://www.themoviedb.org/) to confirm the correct English title and year.
2. **Rename**: Go to **[Settings](../../settings)** --> **Repository name** and rename the repository to the correct `EnglishTitle_Year` format.
3. **Re-run**: Go back to **[Actions](../../actions/workflows/init.yml)** and re-run the workflow.

**Option B: Force Initialization (if the title is not on TMDB)**

1. **Trigger**: Go to **[Actions](../../actions/workflows/init.yml)** and select the **"Initialize"** workflow.
2. **Enable Force Mode**: Before clicking **Run workflow**, check the **“Force init / 强制初始化”** option.
3. **Manual Completion**: This will generate a blank template (info based solely on the repository name). Please verify and manually complete the README content after it runs.
<!-- /block:en -->
