## 解决方法

> [!TIP]
> TMDB 未能找到匹配的影片，有两种处理方式：

**方式一：片名或年份拼写有误**

1. 自行在 [TMDB](https://www.themoviedb.org/) 搜索确认正确的片名与年份
2. 前往仓库 **[Settings](../../settings)** 页面最顶部，将仓库名改为正确的 `英文片名_年份` 格式
3. 改名完成后，回到 **[Actions](../../actions/workflows/init.yml)** 页面重新运行工作流

**方式二：该片确实未被 TMDB 收录，强制初始化**

1. 前往 **[Actions](../../actions/workflows/init.yml)** 页面，重新运行 **"初始化 (Initialize)"** 工作流
2. 运行前勾选 **force_init** 选项
3. 这会生成一个空白模板 README，标题、简介、海报等信息取自仓库名，未经验证，需要你自行手动核实并补全

---

## How to Fix

> [!TIP]
> TMDB couldn't find a matching title — there are two ways to proceed:

**Option 1: Fix a Spelling or Year Mistake**

1. Search [TMDB](https://www.themoviedb.org/) yourself to confirm the correct title and year
2. Go to the top of the repository's **[Settings](../../settings)** page and rename it to the correct `EnglishTitle_Year` format
3. After renaming, go back to **[Actions](../../actions/workflows/init.yml)** and re-run the workflow

**Option 2: The Title Genuinely Isn't on TMDB — Force Initialization**

1. Go to **[Actions](../../actions/workflows/init.yml)** and re-run the **"初始化 (Initialize)"** workflow
2. Before running, check the **force_init** option
3. This generates a blank template README — the title comes from the repository name and is unverified, you'll need to manually verify and fill in the synopsis, poster, and other details yourself
