import re
import requests
import time
import os
import math
import wave
import struct
from GalTransl import LOGGER
from GalTransl.CSentense import CSentense
from GalTransl.GTPlugin import GTextPlugin

try:
    import playsound3
except ImportError:
    LOGGER.warning("缺少依赖包playsound3, 请更新依赖, 否则无法播放声音通知。")

class ServerChanNotifier(GTextPlugin):
    SERVERCHAN_API_URL = 'https://sctapi.ftqq.com/{}.send'
    TELEGRAM_API_URL = '{}/bot{}/sendMessage'

    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        self.start_time = time.time()
        self.pname = plugin_conf["Core"].get("Name", "完成消息推送")
        settings = plugin_conf["Settings"]
        self.push_channels = settings.get("推送渠道", [])
        self.tg_bot_url = settings.get("Telegram_Bot_API_URL", "https://api.telegram.org").rstrip('/')
        self.tg_bot_token = settings.get("Telegram_Bot_Token", "")
        self.tg_chat_id = settings.get("Telegram_Bot_ChatID", "")
        self.serverchan_sendkey = settings.get("ServerChan_SendKey", "")
        self.project_dir = project_conf["project_dir"]
        self.gt_input_dir = os.path.join(self.project_dir, "gt_input")
        self.gt_output_dir = os.path.join(self.project_dir, "gt_output")
        self.project_name = os.path.basename(self.project_dir)
        
        # 声音通知设置
        self.enable_sound = settings.get("启用声音通知", False)
        self.use_custom_audio = settings.get("使用自定义音频", False)
        self.use_openai_tts = settings.get("使用OpenAI TTS", False)
        self.openai_api_key = settings.get("OpenAI_API_Key", "")
        self.openai_api_base_url = settings.get("OpenAI_API_Base_URL", "https://api.openai.com/v1")
        self.openai_tts_voice = settings.get("OpenAI_TTS_Voice", "alloy")

        # 获取声音文件路径
        self.plugin_dir = os.path.abspath(os.path.dirname(__file__))
        self.success_audio_path = os.path.join(self.plugin_dir, "default_sound", "success.wav")
        self.fail_audio_path = os.path.join(self.plugin_dir, "default_sound", "fail.wav")

        if self.use_custom_audio:
            custom_success_path = settings.get("成功音频路径", "")
            custom_fail_path = settings.get("失败音频路径", "")
            if custom_success_path:
                self.success_audio_path = os.path.join(self.plugin_dir, custom_success_path)
            if custom_fail_path:
                self.fail_audio_path = os.path.join(self.plugin_dir, custom_fail_path)

        self._validate_config()

    def _validate_config(self):
        if not self.push_channels:
            LOGGER.warning(f"[{self.pname}] 未设置任何推送渠道，插件将不会发送通知。")
        else:
            LOGGER.debug(f"[{self.pname}] 插件初始化成功。推送渠道: {', '.join(self.push_channels)}")
        
        if self.enable_sound:
            if self.use_custom_audio:
                if not os.path.exists(self.success_audio_path):
                    LOGGER.warning(f"[{self.pname}] 成功音频文件不存在: {self.success_audio_path}")
                if not os.path.exists(self.fail_audio_path):
                    LOGGER.warning(f"[{self.pname}] 失败音频文件不存在: {self.fail_audio_path}")
            elif self.use_openai_tts and not self.openai_api_key:
                LOGGER.warning(f"[{self.pname}] 未设置OpenAI API Key，无法使用OpenAI TTS")

    def before_src_processed(self, tran: CSentense) -> CSentense:
        return tran

    def after_src_processed(self, tran: CSentense) -> CSentense:
        return tran

    def before_dst_processed(self, tran: CSentense) -> CSentense:
        return tran

    def after_dst_processed(self, tran: CSentense) -> CSentense:
        return tran

    def gtp_final(self):
        end_time = time.time()
        total_time = end_time - self.start_time
        total_time_format = time.strftime("%H小时 %M分钟 %S秒", time.gmtime(total_time))

        input_files = set(os.listdir(self.gt_input_dir))
        output_files = set(os.listdir(self.gt_output_dir))
        untranslated_files = input_files - output_files

        title, content = self._generate_notification_content(total_time_format, untranslated_files)

        for channel in self.push_channels:
            if channel == "ServerChan":
                if self.serverchan_sendkey:
                    self.send_serverchan_notification(title, content)
                else:
                    LOGGER.warning(f"[{self.pname}] ServerChan SendKey未设置，无法发送ServerChan通知。")
            elif channel == "Telegram Bot":
                if self.tg_bot_token and self.tg_chat_id:
                    self.send_telegram_notification(title, content)
                else:
                    LOGGER.warning(f"[{self.pname}] Telegram Bot Token或ChatID未设置，无法发送Telegram通知。")
            else:
                LOGGER.warning(f"[{self.pname}] 未知的推送渠道: {channel}")

        if not self.push_channels:
            LOGGER.warning(f"[{self.pname}] 未设置任何推送渠道，跳过发送通知。")

        # 播放声音通知
        if self.enable_sound:
            self.play_notification_sound(title, content)

    def _generate_notification_content(self, total_time: str, untranslated_files: set) -> tuple:
        if not untranslated_files:
            title = f"[GalTransl] {self.project_name} 翻译完成"
            content = f"所有文件的翻译工作已经完成，总耗时：{total_time}"
        else:
            title = f"[GalTransl] {self.project_name} 部分文件翻译完成"
            content = f"以下文件翻译失败，总耗时：{total_time}\n"
            content += "\n".join(f" - {file}" for file in sorted(untranslated_files))
        return title, content

    def send_serverchan_notification(self, title, content):
        api_url = self.SERVERCHAN_API_URL.format(self.serverchan_sendkey)
        data = {
            'text': title,
            'desp': content
        }
        
        try:
            response = requests.post(api_url, data=data)
            response.raise_for_status()
            LOGGER.info(f"[{self.pname}] ServerChan通知发送成功")
        except requests.RequestException as e:
            LOGGER.error(f"[{self.pname}] 发送ServerChan通知时发生错误：{str(e)}")
            if hasattr(e, 'response'):
                LOGGER.error(f"响应内容：{e.response.text}")

    def send_telegram_notification(self, title, content):
        api_url = self.TELEGRAM_API_URL.format(self.tg_bot_url, self.tg_bot_token)
        
        message = f"*{self._escape_markdown(title)}*\n\n{self._escape_markdown(content)}"
        params = {
            "chat_id": self.tg_chat_id,
            "text": message,
            "parse_mode": "MarkdownV2"
        }
        
        try:
            response = requests.post(api_url, params=params)
            response.raise_for_status()
            LOGGER.info(f"[{self.pname}] Telegram Bot通知发送成功")
        except requests.RequestException as e:
            LOGGER.error(f"[{self.pname}] 发送Telegram Bot通知时发生错误：{str(e)}")
            if hasattr(e, 'response'):
                LOGGER.error(f"响应内容：{e.response.text}")

    def play_notification_sound(self, title, content):
        is_success = "所有文件的翻译工作已经完成" in content
        audio_path = self.success_audio_path if is_success else self.fail_audio_path

        if self.use_openai_tts:
            self.play_openai_tts(title, content)
        elif self.use_custom_audio or not self.use_openai_tts:
            if os.path.exists(audio_path):
                self.play_audio(audio_path)
            else:
                LOGGER.warning(f"[{self.pname}] 音频文件不存在: {audio_path}")
                LOGGER.warning(f"[{self.pname}] 播放beep音")
                frequency = 440  # 设置频率为 440 Hz
                duration = 1000  # 设置持续时间为 1000 毫秒
                self.play_beep(frequency, duration)

    def play_openai_tts(self, title, content):
        if not self.openai_api_key:
            LOGGER.warning(f"[{self.pname}] OpenAI API Key未设置，无法使用OpenAI TTS")
            return

        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json"
        }

        full_message = f"{title}。{content}"
        data = {
            "model": "tts-1",
            "input": full_message,
            "voice": self.openai_tts_voice,
            "response_format": "mp3"  # 改为 mp3 格式
        }

        try:
            response = requests.post(f"{self.openai_api_base_url}/audio/speech", headers=headers, json=data)
            response.raise_for_status()
            
            mp3_file = os.path.join(self.project_dir, "openai_tts.mp3")
            with open(mp3_file, "wb") as f:
                f.write(response.content)
            
            LOGGER.debug(f"[{self.pname}] OpenAI TTS音频文件成功保存到: {mp3_file}")
            
            self.play_audio(mp3_file)

            # 成功播放音频后删除临时文件
            os.remove(mp3_file)
            LOGGER.debug(f"[{self.pname}] OpenAI TTS音频文件成功删除: {mp3_file}")
            
        except requests.RequestException as e:
            LOGGER.error(f"[{self.pname}] OpenAI TTS请求失败: {str(e)}")
        except Exception as e:
            LOGGER.error(f"[{self.pname}] 处理或播放音频时发生错误: {str(e)}")

    def play_audio(self, audio_file):
        try:
            playsound3.playsound(audio_file)
            LOGGER.debug(f"[{self.pname}] 音频文件成功播放")
        except Exception as e:
            LOGGER.error(f"[{self.pname}] 播放音频文件时发生错误: {str(e)}")

    def play_beep(self, frequency, duration):
        # 采样参数
        sample_rate = 44100
        num_samples = int(sample_rate * duration / 1000)

        # 生成正弦波
        samples = [int(32767 * math.sin(2 * math.pi * frequency * t / sample_rate)) for t in range(num_samples)]

        # 创建 WAV 文件
        temp_file = "temp_beep.wav"
        with wave.open(temp_file, "w") as wav_file:
            wav_file.setnchannels(1)  # 单声道
            wav_file.setsampwidth(2)  # 2 字节采样
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(struct.pack('{}h'.format(len(samples)), *samples))

        # 播放音频
        playsound3.playsound(temp_file, block=True)  # 改为阻塞模式,确保播放完成

        # 删除临时文件
        os.remove(temp_file)

    @staticmethod
    def _escape_markdown(text):
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)