# Project Console

macOS 原生(Python + Tkinter)桌面「本地项目控制台」:把散落在各处的本地项目
启停命令、端口占用、日志、远程服务器 systemd 服务和前端静态发布,收进一个
双击即用的 App。

纯标准库实现(零第三方运行时依赖),打包后为独立 `.app`,双击即用。

![platform](https://img.shields.io/badge/platform-macOS-lightgrey) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## 功能

- **本地项目管理**:左栏按分组列出受管项目,红绿灯显示本地端口 / 线上健康状态;
  一键执行 start / stop / 自定义脚本,输出进多 tab 日志区。
- **内置终端**:每个项目可开一个真实 zsh(pty),`npm run dev` 这类前台命令
  直接在 App 里跑,Ctrl-C / 关窗口都会把整组进程收干净。
- **远程服务器**:管理一组 SSH 服务器(密钥或密码认证),一键
  `systemctl start/stop/restart`,带连通性测试与健康检查。
- **前端静态发布**(可选):`npm run build:*` 打包 → 上传服务器 incoming
  目录 → 校验 → 原子切换 → 清理,每一步有确认与日志;发布前自动做 Git
  脏工作区 / 未推送检查。
- **目录扫描纳管**:扫描你的项目根目录,按 `package.json` / `pom.xml`
  自动生成条目,不用手写注册表。
- **密钥外置**:服务器密码、网站账号写在与 `projects.json` 同目录的
  `secrets.json`(0600 权限),自动读写,不会被提交到 git。
- **双主题**:玻璃拟态深色 / 浅色主题。

## 快速开始

### 方式一:自己构建

```bash
git clone https://github.com/272jskw26b-dotcom/local-dev-project-manager.git
cd local-dev-project-manager
./build.sh          # 构建并部署到桌面(需要带 tkinter 的 Python 3.10+)
```

`build.sh` 会自动创建虚拟环境、安装 pyinstaller 并打包。如果系统里有多个
Python,可以用环境变量指定解释器:

```bash
PC_PYTHON=/opt/homebrew/bin/python3.13 ./build.sh
```

其他命令:`./build.sh run`(源码运行)/ `build`(只构建)/ `deploy`
(只部署)/ `debug`(带终端的调试版,排查启动失败)/ `clean`。

### 方式二:下载 Release

从 [Releases](https://github.com/272jskw26b-dotcom/local-dev-project-manager/releases)
下载 `Project Console.app`,拖到「应用程序」或桌面双击即可。

## 首次启动

第一次打开 App 时会弹出引导:

1. **选择项目根目录** —— 其下每个子目录(一级为分组、二级为项目)会被扫描,
   按 `package.json` / `pom.xml` 自动识别前端 / 后端 / 纯脚本仓;
2. 确认扫描结果后自动生成 `projects.json`;
3. 也可以点「跳过」载入示例配置(`projects.example.json`),之后手工编辑。

配置文件位置(按优先级):

1. `.app` 旁边的 `projects.json`(便携模式);
2. `~/Library/Application Support/ProjectConsole/projects.json`(默认);
3. 环境变量 `PROJ_CONSOLE_DIR` 指定的目录(最高优先)。

日志区顶部会打印实际生效的注册表与密钥文件路径。

## 配置说明

`projects.json` 顶层结构:

| 字段 | 说明 |
|---|---|
| `work_root` | 项目根目录(「⤵ 同步项目目录」的扫描起点) |
| `targets` | 服务器清单:`{id, name, ssh: "user@host", port, auth: "key"/"password"}`;密码写 secrets.json |
| `entries` | 受管条目清单,见下表 |

条目(`entries[]`)常用字段:

| 字段 | 说明 |
|---|---|
| `group` / `id` / `name` | 分组、唯一 id、显示名 |
| `dir` | 本地项目路径(支持 `~`) |
| `desc` | 备注名 |
| `local.start` / `local.stop` | 本地启停命令(在项目目录下执行) |
| `local.ports` | 端口监听列表 `[{port, label}]`,红绿灯数据源 |
| `local.urls` | 快捷打开的本地页面 |
| `server` | 关联 `targets` 里的服务器 id;系统 tab 显示 systemd 服务启停 |
| `envs[]` | 发布环境:目标服务器 + 前端静态产物发布流程 |
| `maven` / `java_version` | 标记为后端(后端暂不做自动发布) |

完整示例见 [`projects.example.json`](projects.example.json)。

### secrets.json(不要提交)

`targets[].password` 与 `envs[].accounts[]`(网站登录账号)属于个人信息。
保存配置时 App 会自动把它们抽到同目录 `secrets.json`(0600,原子写,自动
备份一代),`projects.json` 里只留干净结构。两个文件都已写在 `.gitignore`。

## 常见问题

**启动后提示找不到 node / npm?** App 启动子进程时会在常见路径
(`/opt/homebrew/bin`、`/usr/local/bin`、nvm/fnm 目录等)补全 PATH;
如果你的工具装在别处,在条目命令里写绝对路径,或用内置终端跑。

**`./build.sh` 报「未找到带 tkinter 的 Python」?** macOS 自带的
`/usr/bin/python3` 不一定带 tkinter。`brew install python-tk@3.13` 或用
`PC_PYTHON` 指定一个带 tkinter 的解释器。

**打包后启动闪退?** 用 `./build.sh debug` 构建带终端的调试版,看完整
traceback。

**想重置配置?** 删掉横幅里打印的那个 `projects.json`(以及同目录
`secrets.json`),重启即重新进入首启引导。

## 许可

[MIT](LICENSE)
