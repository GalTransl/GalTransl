
<div align=center><img width="150" height="150" src="./img/logo.png"/></div>

<h1><p align='center' >GalTransl</p></h1>
<div align=center><img src="https://img.shields.io/github/v/release/XD2333/GalTransl"/>   <img src="https://img.shields.io/github/license/XD2333/GalTransl"/>   <img src="https://img.shields.io/github/stars/XD2333/GalTransl"/></div>
<p align='center' >Visual Novel Automatic Translation Solution Supporting GPT-4/Claude/Deepseek/Sakura and More</p>

  [中文](https://github.com/XD2333/GalTransl/blob/main/README.md)

  GalTransl is a set of visual novel automatic translation tools that combines several minor innovations in basic functions with deep use of GPT prompt engineering to create embedded translation patches. Now featuring a **desktop graphical interface** — no command-line experience needed to complete the full translation workflow.

   <img width="2044" height="1397" alt="image" src="https://github.com/user-attachments/assets/f85e4782-e53e-4b03-ae24-cd77b453c6e3" />

## Preface
&ensp;&ensp;&ensp;&ensp;The core of GalTransl is a set of automatic translation scripts that solves most of the known problems in using GPT to automatically translate visual novels, and greatly improves the overall translation quality. By combining with other projects, it connects the whole process of making patches, lowering the entry barrier.

  * Features:
  1. 🖥️ **Desktop GUI** — Modern desktop app built with Tauri + React, no command-line needed. Supports dark mode, custom backgrounds, multi-project management, and more
  2. Supports **GPT-4/Claude/Deepseek/Sakura** and other LLMs, with improved translation quality through prompt engineering
  3. Pioneering **GPT Dictionary** system — lets GPT understand character settings, accurately translate names, pronouns, and new words
  4. Flexible automatic dictionary system with pre-translation, post-translation, and conditional dictionaries
  5. Real-time cache saving, automatic breakpoint continuation
  6. Combined with other projects to support one-click unpacking and injection of multi-engine scripts
  7. Supports translating srt, lrc, vtt subtitle files, mtool json, t++ excel, and epub files
  8. 🤗 [GalTransl-7B-v3.5](https://huggingface.co/SakuraLLM/GalTransl-7B-v2) — a local model specifically optimized for visual novel translation, deployable on 6G+ VRAM GPUs
  9. 🤗 [GalTransl-14B-v3](https://huggingface.co/SakuraLLM/Sakura-GalTransl-14B-v3) — the 14B version with better overall quality thanks to a larger base model and improved alignment training

<b>❗❗When publishing translations made with this tool without full manual proofreading/polishing, please clearly label them as "GPT translation/AI translation patch", not "personal translation" or "AI localization".</b>

## Recent Updates
* 2026.4: Updated v7, added **desktop GUI** (Tauri + React) with dark mode, custom backgrounds, multi-project management, visual translation workbench, etc.
* 2025.5: Updated v6, added ForGal translation template, GalTransl-14B-v3 model
* 2024.5: Updated v5, added GalTransl-7B model, multiple file type support
* 2024.2: Updated v4, plugin system support
* 2023.12: Updated v3, file-based multithreading by @ryank231231
* 2023.7: Updated v2, major code refactoring by @ryank231231
* 2023.6: v1 initial release

## Navigation
* [Environment Preparation](https://github.com/XD2333/GalTransl#environment-preparation): Installing environment and software
* [Getting Started Tutorial](https://github.com/XD2333/GalTransl#getting-started-tutorial): Full process introduction on making a machine-translated patch
* [Configuration and Engine Settings](https://github.com/XD2333/GalTransl#configuration-and-engine-settings): Details on configuring translation engine APIs
* [GalTransl Core Features](https://github.com/XD2333/GalTransl#galtransl-core-features): GPT dictionary, cache, ordinary dictionary, problem finding, etc.
* Further tutorials have been [moved to Wiki](https://github.com/xd2333/GalTransl/wiki)

## Environment Preparation
  * **Desktop Version (Recommended)**
  Download the latest release zip from [Release](https://github.com/XD2333/GalTransl/releases/), extract it, and double-click `GalTransl Desktop.exe` to start. **No Python or any dependencies required.** The desktop app automatically starts the backend service.

  * **Command-line Version (Developers / Advanced Users)**
  To use the command-line version or participate in development:

  1. [Download this project](https://github.com/XD2333/GalTransl/releases/) or clone the repository, extract to any location
  2. Install Python 3.11.9. [Download](https://www.python.org/downloads/release/python-3119/)
  **Check "Add Python to PATH" during installation**
  3. Install Python dependencies: double-click `安装、更新依赖.bat`, or run `pip install -r requirements.txt`
  4. (Desktop development) Install Node.js, run `npm install` in the `desktop` directory, then run `run_desktop_dev.bat`


## Practical Tools
| Name | Description |
| --- | --- |
| GARbro | Engine tool: Universal unpacker. [Download](https://github.com/morkt/GARbro/releases/download/v1.5.44/GARbro-v1.5.44.2904.rar) |
| [KirikiriTools](https://github.com/arcusmaximus/KirikiriTools) | Engine tool: Krkr, krkrz extraction and injection tool |
| [UniversalInjectorFramework](https://github.com/AtomCrafty/UniversalInjectorFramework) | Engine tool: Shift-JIS tunnel, Shift-JIS replacement mode universal injection framework |
| [VNTextProxy](https://github.com/arcusmaximus/VNTranslationTools) | Engine tool: Shift-JIS tunnel mode universal injection framework |
| GalTransl_DumpInjector | Script tool: [VNTextPatch](https://github.com/arcusmaximus/VNTranslationTools) GUI, comprehensive script text extraction/injection tool |
| [SExtractor](https://github.com/satan53x/SExtractor) | Script tool: Comprehensive script text extraction/injection tool |
| [msg-tool](https://github.com/lifegpc/msg-tool) | Script tool: Comprehensive script text extraction/injection tool |
| [DBTXT2Json_jp](https://github.com/XD2333/DBTXT2Json_jp) | Script tool: Double-line text and json_jp conversion script |
| [EmEditor](https://www.emeditor.com/) | Text tool: Powerful text editor, mainly for editing cache files |
| [VSCode](https://code.visualstudio.com/) | Text tool: Powerful text editor, mainly for editing cache files |
| [KeywordGacha](https://github.com/neavo/KeywordGacha) | Text tool: Automatic glossary generation using OpenAI-compatible API |

## Getting started tutorial
The general process of making a visual novel embedded translation patch is:

1. Identify the engine -> unpack the resource pack to get the script -> go to 2.
2. Dump the script into original language text -> translate into target language text -> build the target script -> go to 3
3. Pack as resource pack/non-pack -> go to 4
4. If the engine supports Unicode, just play -> if the engine uses Shift-JIS, try 2 approaches to display Chinese

* It is recommended to only run the translation of the first file, or just add some strings randomly, and return to the game to confirm that it can be displayed normally before translating all.

(Click to expand detailed instructions)
<details>

<summary>

### Part 1 Identification and Unpacking

</summary>
Identifying the engine is actually very simple. Usually, using GARbro to open any resource pack in the game directory, the engine name will be displayed in the lower left corner of the status bar.

Or, refer to the [supported formats](https://morkt.github.io/GARbro/supported.html), and compare the suffixes of the resource packs.

Scripts are usually in some resource packs with obvious keywords, or in directories with obvious keywords in the resource packs, such as: scene, scenario, message, script, etc. And scripts are usually divided into obvious chapters and characters, some of which are also divided into main route and erotic (such as with _h), usually you can find them by looking through a few resource packs.

Especially for the new krkrz engine, GARbro can no longer open the resource pack, you can use the [KrkrzExtract project](https://github.com/xmoezzz/KrkrzExtract/releases/tag/1.0.0.0), drag the game to the exe to start. Then download a full cg save, and skip all the plots directly, you can also get the script file.

</details>
<details>

<summary>

### Part 2 Extraction and Translation

</summary>

* **【2.1. Extract script text】**
Usually, this project is combined with [VNTextPatch tool](https://github.com/arcusmaximus/VNTranslationTools) to unpack the script. VNTextPatch is a universal tool developed by arcusmaximus that supports extraction and injection of scripts for [many engines](https://github.com/arcusmaximus/VNTranslationTools#vntextpatch).

VNTextPatch is operated using cmd. In order to reduce the difficulty of getting started, a graphical interface is available in the useful_tools/GalTransl_DumpInjector folder. Click GalTransl_DumpInjector.exe to run.

Now, you only need to select the original script directory, and then select the directory where you want to save the extracted original json. Here we usually put the original script in a folder called script_jp, and then create a new gt_input directory to store the extracted script:

![Picture 1](./img/img_dumper.png)

GalTransl uses name-message JSON format for input, processing and output throughout. The extracted json file looks like this:

```json
[
  {
    "name": "咲來",
    "message": "「ってか、白鷺学園だったらあたしと一緒じゃん。\r\nセンパイだったんですねー」"
  }
]
```
Each {} object is a sentence, `message` is the message content, and if the object also has a `name`, it means it is a dialogue. **When names can be correctly extracted, GalTransl's translation quality will be better.**

PS. GalTransl only supports input of json files in a specified format, but that does not mean that GalTransl is bound to the VNTextPatch tool. You can also use [SExtractor](https://github.com/satan53x/SExtractor) tool, which now supports exporting GalTransl's name-message format JSON.

* **【2.2. Using Desktop GUI (Recommended)】**
Download the latest release from [Release](https://github.com/XD2333/GalTransl/releases/), extract it, and double-click `GalTransl Desktop.exe` to start. The desktop app automatically starts the backend service.

Basic workflow after starting:
1. **New Project**: Click "New Project" on the homepage, select project location, import files to translate
2. **Configure Backend**: In the new project wizard, select translation backend (e.g. Deepseek, OpenAI, etc.), fill in API Key and Endpoint. You can also pre-configure multiple backend profiles in the "Backend Profiles" page on the left sidebar
3. **Set Dictionaries**: Configure GPT dictionary and regular dictionaries in the project's "Project Dictionary" page (at minimum, configure a name dictionary)
4. **Start Translation**: Click start translation on the "Translation Workbench" page, monitor progress and results in real-time
5. **Review Cache & Problems**: After translation, check automatic error finding results on the "Cache & Problems" page, fix cache entries and regenerate

The desktop app supports opening multiple projects simultaneously, dark mode, custom backgrounds, and more. These can be adjusted in the "Settings" page.

* **【2.2b. Using Command Line (Advanced Users)】**
To use the command-line version, rename `config.inc.yaml` to `config.yaml` in the `sampleProject` folder, put the source json files in the `gt_input` folder, and edit `config.yaml` to configure the translation backend:

```yaml
# Translation backend settings
backendSpecific:
  OpenAI-Compatible: # (ForGal/ForNovel/r1/Gendic) OpenAI API compatible interface
    tokens:
      - token: sk-example-key1
        endpoint: https://api.deepseek.com # Request URL
        modelName: deepseek-chat
      - token: sk-example-key2
        endpoint: https://openrouter.ai/api/v1/chat/completions
        modelName: deepseek/deepseek-chat-v3-0324:free
        stream: true
```

After modifying the project settings, make sure you have installed the required dependencies (see Environment Preparation), then double-click `run_GalTransl_terminal.bat` and enter the project path to start translating.

**However**, it is not recommended to start translating right away. Please at least learn about [GPT Dictionary usage](https://github.com/XD2333/GalTransl#gpt-dictionary) first, or use GenDic to generate a name dictionary, so as to ensure basic translation quality.

After translation is complete, **remember to review the cache**, as LLMs often make mistakes. GalTransl automatically finds common problems and records them in the cache. You can fix the cache and rerun the program to regenerate the result json. See [Automatic Error Finding and Translation Cache](https://github.com/XD2333/GalTransl#automatic-error-finding).

* **【2.3. Build target language script】**
If you used the GalTransl extraction and injection tool to extract the script, build the same way: select the original script directory, translated json directory, and target script save directory, then click 'inject' to inject the text back into the script.

Note:
1. The target script save directory is usually called script_cn
2. Generally use the same tool for both export and import. So test both import and export before starting translation.

</details>

<details>

<summary>

### Part 3 Pack or Non-pack

</summary>

After building the target language script, the next step is to find a way to make the game read it. Most mainstream engines support non-pack reading.

Especially for krkr/krkrz engine, you can use [KirikiriTools tool](https://github.com/arcusmaximus/KirikiriTools) by arcusmaximus, download the version.dll inside, put it in the game directory, then create a new "unencrypted" folder in the game directory, put the script in, and krkr can read it.

</details>

<details>

<summary>

### Part 4 Engines and Encoding

</summary>

First, you need to understand the basics of Unicode, Shift-JIS (SJIS), and GBK encoding. If the engine you are working with supports Unicode encoding (e.g. krkr, Artemis engine), you can generally play directly. But if the engine uses SJIS encoding, you will see garbled text and need to try 2 approaches:

**Route 1**: Inject scripts using GBK encoding, then modify the engine program to support GBK encoding

**Route 2**: Still inject scripts using JIS encoding, but use JIS tunnel or JIS replacement (recommended) combined with universal injection DLL to dynamically replace characters at runtime to display Chinese

For detailed instructions on JIS tunnel and JIS replacement, please refer to the [Chinese README](https://github.com/XD2333/GalTransl#第四章-引擎与编码) as these techniques are primarily relevant for Chinese translation.

</details>

## GalTransl Core Features
Introduces GPT dictionary, cache, ordinary dictionary, problem finding and other functions.
(Click to expand detailed instructions)
<details>

<summary>

### GPT Dictionary
The GPT dictionary system is a key function to improve translation quality when using GPT. It greatly improves translation quality by supplementing character settings and word explanations.

</summary>

* For example, you can pre-define the target language translation of each character name here, and explain the character's setting, such as gender, approximate age, occupation, etc. By automatically feeding GPT these settings, you can automatically adjust the appropriate pronouns, titles, etc., and fix the translation of names when they are in kana.
* Or, you can supplement some words that GPT always translates incorrectly. If you provide some explanation, it will understand better.

---

* Learn how to use GPT dictionary to feed character settings through the following example. The format of each line is `Source[Tab]Target[Tab]Explanation(optional)`, note the connector is **TAB**
```
フラン	Flan	name, lady, teacher
笠間	Kasama	笠間 陽菜乃's lastname, girl
陽菜乃	Hinano	笠間 陽菜乃's firstname, girl
瞬	Shun	player's name, boy
$str20	$str20	player's codename, boy
```
These dictionary entries define characters:
* The first one tells GPT: "The translation of フラン is Flan, this is a name, a lady, a teacher". This way GPT will translate フラン先生 as Flan teacher instead of Flan doctor.
* The second and third ones are the Japanese surname and given name of the same person. Names must be written in two lines, otherwise GPT may not recognize them.
* The fourth one is the recommended way to write the **protagonist's name**. **Note: even if the source and target are the same, repeat them**
* The fifth one is for when the protagonist uses a placeholder instead of a name in the script.

---

* Learn how to use GPT dictionary to feed new words:
```
大家さん  landlord
あたし	I/ic	use 'ic' when being cute
```

In the program directory, there is a "General GPT Dictionary.txt" in the `Dict` folder, and a "Project GPT Dictionary.txt" in the project folder. Generally, name definitions are written in the project dictionary, and common words that improve translation quality are written in the general dictionary.

Only when the name and sentence sent to GPT this time contain this word, will this word's explanation be sent into this round of conversation. **Don't add everything to it** — it is recommended to only write **the settings of each character** and **words that are always translated wrong**.

The dictionary will be dynamically displayed in each request when running:

![img_start](./img/img_start.png)

</details>

<details>

<summary>

### Ordinary Dictionary
In GalTransl, ordinary dictionary is divided into "pre-translation dictionary" and "post-translation dictionary". Pre-translation dictionary does a-to-b replacement of original script before translation, and post-translation dictionary does a-to-b replacement of translated script after translation.

</summary>

Pre-translation dictionary is mostly used for unclear speech correction, and if multiple words represent the same meaning, you can use pre-translation dictionary to unify them first.

Post-translation dictionary is more common, but here we have an improved "conditional dictionary". Conditional dictionary adds a step of judgment before replacing, to avoid misreplacement and overreplacement.
Each line format is `pre_jp/post_jp[tab]judgment word[tab]search word[tab]replacement word`
* pre_jp/post_jp indicates the position where the judgment word is searched
* Judgment word: If the judgment word is found in the search position, the replacement will be activated.
* Judgment word can be prefixed with "!" to mean "replace if not present", otherwise it usually means "replace if present".
* Judgment word can use `[or]` or `[and]` keywords to connect

</details>

<details>

<summary>

### Translation Cache
After starting the translation, you can find the translation cache in the transl_cache directory.
</summary>

The translation cache corresponds to json_jp one by one. During the translation process, the translation result will be written to the cache first. When a file is translated, it will appear in json_cn.

Key points:
1. When you want to re-translate a sentence, open the corresponding translation cache file and delete the whole line of pre_zh for that sentence (**do not leave a blank line**)
2. When you want to re-translate a whole paragraph, just delete the corresponding number of object blocks. When you want to re-translate a file, just delete the corresponding translation cache file.
3. When GalTransl is translating, do not modify the cache of the file being translated. It will be overwritten anyway.
4. json_cn result file = pre_zh/proofread_zh in translation cache + post-translation dictionary replacement + restore dialogue box
5. When the new post_jp is inconsistent with the post_jp in the cache, it will trigger re-translation, which usually happens when a new pre-translation dictionary is added

```json
{
    "index": 4,
    "name": "",
    "pre_jp": "欠品していたコーヒー豆を受け取ったまでは良かったが、\r\n帰り道を歩いていると汗が吹き出してくる。",
    "post_jp": "欠品していたコーヒー豆を受け取ったまでは良かったが、\r\n帰り道を歩いていると汗が吹き出してくる。",
    "pre_zh": "领取了缺货的咖啡豆还好，\r\n但是走在回去的路上就汗流浃背了。",
    "proofread_zh": "领了缺货的咖啡豆倒是没问题，\r\n可是走在回去的路上，汗水就冒了出来。",
    "trans_by": "NewBing",
    "proofread_by": "NewBing",
}
```

Field explanations:
* `index` — serial number
* `name` — character name
* `pre_jp` — original text
* `post_jp` — processed text (generally: pre_jp with dialogue box removed + pre-translation dictionary replacement)
* `pre_zh` — raw translation
* `proofread_zh` — proofread translation
* `trans_by` — translation engine/translator
* `proofread_by` — proofreading engine/proofreader
* `problem` — stored problems (see Automatic Error Finding)
* `post_zh_preview` — for previewing json_cn, but **modifying it will not apply to json_cn**; modify `pre_zh`/`proofread_zh` instead

After determining the content that needs to be modified, directly modify the `pre_zh` or `proofread_zh` of the corresponding sentence, and then rerun the program to generate a new json_cn.

</details>

<details>

<summary>

### Automatic Error Finding

GalTransl has built a system of automatically finding problems based on rules based on long-term observation of translation results.

</summary>
The error finding system is enabled in the `config.yaml` of each project. The default configuration is:

```yaml
# Automatic problem analysis config
problemAnalyze:
  problemList: # Problem checklist
    - 词频过高 # Repeated more than 20 times
    - 标点错漏 # Punctuation added or missing
    - 残留日文 # Japanese hiragana/katakana remaining
    - 丢失换行 # Missing line breaks
    - 多加换行 # More line breaks than original
    - 比日文长 # 1.3x longer than Japanese
    - 字典使用 # Not following GPT dictionary requirements
    - 语言不通 # Suspected not translated to target language
```

Currently supports finding the above problems. Some items are commented out with #, you can uncomment to enable, or add # to disable.

After finding problems, they are stored in the translation cache. You can use Emeditor or VSCode to batch search the "problem" keyword to see all current problems, and correct them by modifying the cache.

(New) You can also configure `retranslKey` in config.yaml to batch re-translate a specific problem, e.g. `retranslKey: "残留日文"`

</details>

## Configuration and Engine Settings

The desktop GUI manages translation backend configuration through the graphical interface (the "Backend Profiles" page on the left sidebar), no manual YAML editing required. Project-level configuration can be modified in the "Config Editor" page.

For the command-line version, detailed settings can be found directly in the `config.yaml` file comments, which are now quite comprehensive.
