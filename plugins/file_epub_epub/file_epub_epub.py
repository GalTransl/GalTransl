import re
import os
import shutil
import zipfile
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin

try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    LOGGER.warning("缺少依赖包ebooklib, 请更新依赖")
try:
    from bs4 import BeautifulSoup
except ImportError:
    LOGGER.warning("缺少依赖包beautifulsoup4, 请更新依赖")

class FilePlugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded. 在插件加载时被调用。
        :param plugin_conf: The settings for the plugin. 插件yaml中所有设置的dict。
        :param project_conf: The settings for the project. 项目yaml中common下设置的dict。
        """
        self.pname = plugin_conf["Core"].get("Name", "")
        settings = plugin_conf["Settings"]
        self.是否自动识别名称 = settings.get("是否自动识别名称", False)
        self.名称识别拼接方案 = settings.get("名称识别拼接方案", "{name}\n「{message}」")
        self.名称识别正则表达式 = re.compile(settings.get("名称识别正则表达式", r"^(?P<name>.*?)「(?P<message>.*?)」$"), re.DOTALL)
        self.原文颜色 = settings.get("原文颜色", "#808080")
        self.缩小比例 = settings.get("缩小比例", "0.8")
        self.双语显示 = settings.get("双语显示", True)
        self.project_dir = project_conf["project_dir"]
        self.是否拆分文件以支持单文件多线程 = settings.get("是否拆分文件以支持单文件多线程", False)

        if self.是否拆分文件以支持单文件多线程:
            self.extract_epub()

    def extract_epub(self):
        """
        解压EPUB文件,将所有HTML和XHTML文件复制到 gt_input 文件夹,不保留目录结构。
        记录每个HTML和XHTML文件的原始相对路径信息,以便后续重建目录结构。
        多个epub文件会增加前缀以区分。
        """
        input_dir = os.path.join(self.project_dir, "gt_input")
        epub_files = [file for file in os.listdir(input_dir) if file.endswith(".epub")]

        self.epub_file_info = {}

        for epub_file in epub_files:
            epub_name = os.path.splitext(epub_file)[0]
            epub_path = os.path.join(input_dir, epub_file)
            temp_dir = os.path.join(self.project_dir, "temp", epub_name)
            os.makedirs(temp_dir, exist_ok=True)

            with zipfile.ZipFile(epub_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)

            self.epub_file_info[epub_name] = {}
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.endswith((".html", ".xhtml")):
                        file_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_path, temp_dir)
                        key = f"{epub_name}_{file}"
                        self.epub_file_info[epub_name][key] = rel_path
                        shutil.copy(file_path, os.path.join(input_dir, key))

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load. 文件路径。
        :return: A list of objects with message and name(optional). 返回一个包含message和name(可空)的对象列表。
        """
        if self.是否拆分文件以支持单文件多线程 and file_path.endswith((".html", ".xhtml")):
            return self.read_html_xhtml_to_json(file_path)
        else:
            if not file_path.endswith(".epub"):
                raise TypeError("File type not supported.")
            if file_path.endswith(".epub") and self.是否拆分文件以支持单文件多线程:
                raise TypeError("你开启了文件拆分，跳过epub文件本身。")
            return self.read_epub_to_json(file_path)

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file. 保存文件时被调用。
        :param file_path: The path of the file to save. 保存文件路径。
        :param transl_json: A list of objects same as the return of load_file(). load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        if self.是否拆分文件以支持单文件多线程:
            self.save_html_xhtml_file(file_path, transl_json)
        else:
            self.save_epub_file(file_path, transl_json)

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作,例如输出提示信息。
        """
        if self.是否拆分文件以支持单文件多线程:
            LOGGER.info(f"[{self.pname}] 正在重建EPUB文件……")
            self.rebuild_epub()

    def read_html_xhtml_to_json(self, file_path: str) -> list:
        """
        读取HTML或XHTML文件,返回一个包含message和name(可空)的对象列表。
        :param file_path: HTML或XHTML文件路径。
        :return: 包含message和name(可空)的对象列表。
        """

        from ebooklib.epub import EpubHtml
        from ebooklib.epub import CHAPTER_XML
        json_list = []
        
        # 模拟 EpubBook 对象
        class MockEpubBook:
            def __init__(self):
                self.IDENTIFIER_ID = 'id'
                self.language = 'jp'
                self.title = 'Mock Book'
                self.direction = None

            def get_template(self, name):
                return CHAPTER_XML  # 这是在epub库中定义的常量

            def get_metadata(self, namespace, name):
                return []

        # 创建一个临时的 EpubHtml 对象
        temp_item = EpubHtml()
        temp_item.file_name = os.path.basename(file_path)
        temp_item.book = MockEpubBook()  # 设置模拟的 book 属性
        temp_item.lang = 'en'  # 设置语言属性
        temp_item.direction = None  # 设置方向属性
        
        # 读取文件内容
        with open(file_path, 'rb') as file:
            temp_item.content = file.read()
        
        # 使用 get_content 方法获取处理后的内容
        html_content = temp_item.get_content()
        
        # 如果 html_content 是 bytes 类型，解码为字符串
        if isinstance(html_content, bytes):
            html_content = html_content.decode('utf-8')
        
        # 检查是否只包含图片
        if '<img' in html_content and len(re.findall(r'<p', html_content)) <= 1:
            # 如果只包含图片,返回一个包含原始HTML内容的对象
            return [{"index": 1, "name": "", "message": "", "original_message": "", "html": html_content}]
        
        paragraphs = self.extract_paragraphs(html_content)
        if not paragraphs or paragraphs == [''] or paragraphs == ['\n'] or paragraphs == []:
            return [{"index": 1, "name": "", "message": "", "original_message": "", "html": ""}]
        
        i = 1
        for p in paragraphs:
            cleaned_text, text_content, _ = self.process_paragraph(p)

            if not text_content.strip():
                continue

            name, message = self.extract_name_message(text_content)
            json_list.append({
                "index": i,
                "name": name,
                "message": message,
                "original_message": text_content,
                "html": cleaned_text,
            })
            i += 1

        return json_list


    def read_epub_to_json(self, file_path: str) -> list:
        """
        读取EPUB文件,返回一个包含message和name(可空)的对象列表。
        :param file_path: EPUB文件路径。
        :return: 包含message和name(可空)的对象列表。
        """
        json_list = []
        book = epub.read_epub(file_path)
        i = 1

        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                html_content = item.get_content().decode('utf-8')
                paragraphs = self.extract_paragraphs(html_content)

                for p in paragraphs:
                    cleaned_text, text_content, item_id = self.process_paragraph(
                        p, item)

                    if not text_content.strip():
                        continue

                    name, message = self.extract_name_message(text_content)
                    json_list.append({
                        "index": i,
                        "name": name,
                        "message": message,
                        "original_message": text_content,
                        "html": cleaned_text,
                        "item_id": item_id,
                    })
                    i += 1
        return json_list

    def extract_paragraphs(self, html_content: str) -> list:
        """
        从HTML内容中提取<p>标签及其内容。
        :param html_content: HTML内容。
        :return: 包含<p>标签及其内容的列表。
        """
        p_pattern = r'<p[^>/]*>(.*?)</p>|<p[^>/]*/>'
        paragraphs = re.findall(p_pattern, html_content, re.DOTALL)
        return [match for match in paragraphs if match.strip()]

    def process_paragraph(self, p: str, item: epub.EpubItem = None) -> tuple:
        """
        处理段落,提取文本内容和相关信息。
        :param p: 段落内容。
        :param item: epub.EpubItem对象。
        :return: 包含清理后的段落内容、文本内容和item_id的元组。
        """
        cleaned_text = p
        p_html = f"<p>{p}</p>"
        soup = BeautifulSoup(p_html, 'html.parser')
        soup.prettify(formatter="html")
        text_content = soup.get_text()

        text_content = text_content.lstrip()
        cleaned_text = cleaned_text.lstrip()

        item_id = item.get_id() if item else None

        return cleaned_text, text_content, item_id

    def extract_name_message(self, text_content: str) -> tuple:
        """
        从文本内容中提取名称和消息。
        :param text_content: 文本内容。
        :return: 包含名称和消息的元组。
        """
        name = ""
        message = text_content
        if self.是否自动识别名称:
            match = self.名称识别正则表达式.search(text_content)
            if match:
                name = match.group("name").strip()
                message = match.group("message").strip()
        return name, message

    def save_html_xhtml_file(self, file_path: str, transl_json: list):
        """
        保存翻译后的HTML或XHTML文件。
        :param file_path: 保存文件路径。
        :param transl_json: 包含翻译内容的JSON对象列表。
        """
        if len(transl_json) == 1 and not transl_json[0]['message'] and transl_json[0]['html']:
            # 如果只有一个对象,且message为空但html不为空,说明是只包含图片的文件
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(transl_json[0]['html'])
            return

        text_dict = {item['index']: item for item in transl_json if 'index' in item}

        input_file_path = file_path.replace("gt_output", "gt_input")
        with open(input_file_path, 'r', encoding='utf-8') as file:
            content_html = file.read()

        if not content_html.strip():
            # 如果输入文件为空,直接写入空文件
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write('')
            return

        content_html = self.replace_content(content_html, text_dict)

        output_path = file_path
        with open(output_path, 'w', encoding='utf-8') as file:
            file.write(content_html)

    def save_epub_file(self, file_path: str, transl_json: list):
        """
        保存翻译后的EPUB文件。
        :param file_path: 保存文件路径。
        :param transl_json: 包含翻译内容的JSON对象列表。
        """
        text_dict = {item['index']: item for item in transl_json if 'index' in item}
        output_path = file_path
        shutil.copy(file_path.replace("gt_output", "gt_input"), output_path)

        book = epub.read_epub(file_path)
        parent_path = os.path.dirname(file_path)
        extract_path = os.path.join(parent_path, 'EpubCache')

        if not os.path.exists(extract_path):
            os.makedirs(extract_path)

        with zipfile.ZipFile(file_path, 'r') as epub_file:
            epub_file.extractall(extract_path)

        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                item_id = item.get_id()
                file_name = os.path.basename(item.get_name())

                the_file_path = self.find_file_path(extract_path, file_name)

                with open(the_file_path, 'r', encoding='utf-8') as file:
                    content_html = file.read()

                content_html = self.replace_content(content_html, text_dict, item_id)

                soup = BeautifulSoup(content_html, 'html.parser')
                soup.prettify(formatter="html")
                content_html = str(soup)

                with open(the_file_path, 'w', encoding='utf-8') as file:
                    file.write(content_html)

        modified_epub_file = file_path.rsplit('.', 1)[0] + '_translated.epub'
        with zipfile.ZipFile(modified_epub_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(extract_path):
                for file in files:
                    full_file_path = os.path.join(root, file)
                    relative_file_path = os.path.relpath(full_file_path, extract_path)
                    zipf.write(full_file_path, relative_file_path)

        os.remove(file_path)
        shutil.rmtree(extract_path)

    def find_file_path(self, extract_path: str, file_name: str) -> str:
        """
        在提取路径中查找指定文件名的文件路径。
        :param extract_path: 提取路径。
        :param file_name: 文件名。
        :return: 文件路径。
        """
        for root, dirs, files in os.walk(extract_path):
            for filename in files:
                if filename == file_name:
                    return os.path.join(root, filename)
        return ""

    def replace_content(self, content_html: str, text_dict: dict, item_id: str = None) -> str:
        """
        替换HTML内容中的原文为翻译后的内容。
        :param content_html: HTML内容。
        :param text_dict: 包含翻译内容的字典。
        :param item_id: 项目ID。
        :return: 替换后的HTML内容。
        """
        for index, content in text_dict.items():
            if item_id is None or item_id == content.get('item_id'):
                original_name = content['name']
                original_message = content['original_message']
                translated = content['message']
                html = content['html']
                html = str(html)
                html = html.replace("&#13;\n\t\t\t\t", "")

                if (re.match(r'^(?:<a(?:\s[^>]*?)?>[^<]*?</a>)*$', html) is not None):
                    a_tag_pattern = re.compile(r'<a[^>]*>(.*?)</a>')
                    matches = a_tag_pattern.findall(html)
                    if len(matches) == 1:
                        html = matches[0]

                if (original_message and translated):
                    if self.是否自动识别名称 and original_name:
                        original_formatted = self.名称识别拼接方案.format(name=original_name, message=original_message)
                        translated_formatted = self.名称识别拼接方案.format(name=original_name, message=translated)
                    else:
                        original_formatted = original_message
                        translated_formatted = translated

                    if self.双语显示:
                        bilingual_text = f'<p>{translated_formatted}<br><span style="color: {self.原文颜色}; font-size: {self.缩小比例}em;">{original_formatted}</span></p><br />'
                    else:
                        bilingual_text = f'<p>{translated_formatted}</p>'

                    content_html = content_html.replace(html, bilingual_text, 1)
        return content_html

    def rebuild_epub(self):
        """
        重建EPUB文件。
        从原始 temp 目录中复制所有非XHTML文件到新的目录结构中。
        从 gt_output 文件夹中恢复翻译后的XHTML文件的原始相对路径。
        最终打包重建EPUB文件。
        """
        temp_dir = os.path.join(self.project_dir, "temp")
        epub_rebuild_dir = os.path.join(self.project_dir, "epub_rebuild")
        os.makedirs(epub_rebuild_dir, exist_ok=True)

        gt_input = os.path.join(self.project_dir, "gt_input")
        gt_output = os.path.join(self.project_dir, "gt_output")
        epub_files = [file for file in os.listdir(gt_input) if file.endswith(".epub")]

        for epub_name in self.epub_file_info.keys():
            # Copy non-xhtml files and directories from temp dir to epub_rebuild_dir
            temp_subdir = os.path.join(temp_dir, epub_name)
            for root, dirs, files in os.walk(temp_subdir):
                for file in files:
                    if not file.endswith(".xhtml"):
                        source_path = os.path.join(root, file)
                        rel_path = os.path.relpath(source_path, temp_subdir)
                        target_path = os.path.join(epub_rebuild_dir, epub_name, rel_path)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        shutil.copy(source_path, target_path)

            # Move back the translated xhtml files with corrected names
            for file, rel_path in self.epub_file_info[epub_name].items():
                source_path = os.path.join(gt_output, file)
                target_path = os.path.join(epub_rebuild_dir, epub_name, rel_path)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy(source_path, target_path)

            # Rebuild EPUB
            epub_file = f"{epub_name}.epub"
            output_path = os.path.join(gt_output, f"{epub_name}_translated.epub")
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(os.path.join(epub_rebuild_dir, epub_name)):
                    for file in files:
                        file_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_path, os.path.join(epub_rebuild_dir, epub_name))
                        zipf.write(file_path, rel_path)

        shutil.rmtree(temp_dir)
        shutil.rmtree(epub_rebuild_dir)
        for file in os.listdir(gt_output):
            if file.endswith((".xhtml", ".html")): 
                os.remove(os.path.join(gt_output, file))
        for file in os.listdir(gt_input):
            if file.endswith((".xhtml", ".html")): 
                os.remove(os.path.join(gt_input, file))
        LOGGER.info("EPUB文件重建完成，缓存已经清理。")