import orjson, re
import os
from GalTransl import LOGGER
from GalTransl.GTPlugin import GFilePlugin


class file_plugin(GFilePlugin):
    def gtp_init(self, plugin_conf: dict, project_conf: dict):
        """
        This method is called when the plugin is loaded.在插件加载时被调用。
        :param plugin_conf: The settings for the plugin.插件yaml中所有设置的dict。
        :param project_conf: The settings for the project.项目yaml中common下设置的dict。
        """
        self.chrf_all_stats = []  # 存储所有句子的统计数据
        self.chrf_file_stats = {}  # 按文件存储统计数据
        self.line_count = 0
        settings = plugin_conf["Settings"]
        self.task=settings.get("task","").lower()
        self.chrf_ngram=settings.get("chrf_ngram",6)
        self.chrf_beta=settings.get("chrf_beta",2)

        self.galbench=False

        self.comet_model_name=settings.get("comet_model_name","Unbabel/wmt22-comet-da")
        self.comet_batch_size=settings.get("comet_batch_size",8)
        self.comet_file_stats={}
        self.comet_calculator=None

        if "cutoff_rate" in self.task:
            self.cutoff_count=0


        pass                                          

    def load_file(self, file_path: str) -> list:
        """
        This method is called to load a file.
        加载文件时被调用。
        :param file_path: The path of the file to load.文件路径。
        :return: A list of objects with message and name(optional).返回一个包含message和name(可空)的对象列表。
        """
        if not file_path.endswith(".json"):
            # 检查不支持的文件类型并抛出TypeError
            raise TypeError("请检查filePlugin的配置并选择合适的文件插件.")
        with open(file_path, "r", encoding="utf-8") as f:
            json_list = orjson.loads(f.read())
        
        # 复制一份原文
        for item in json_list:
            item['src'] = item['message']

        return json_list

    def save_file(self, file_path: str, transl_json: list):
        """
        This method is called to save a file.
        保存文件时被调用。
        :param file_path: The path of the file to save.保存文件路径
        :param transl_json: A list of objects same as the return of load_file().load_file提供的json在翻译message和name后的结果。
        :return: None.
        """
        file_stats = []
        src_list=[]
        mt_list=[]
        ref_list=[]
        transl_json_copy=transl_json.copy()

        
        # 收集该文件所有句子的统计数据
        for item in transl_json:
            message = item['message']
            mt=message
            ref = item['ref']
            src=item['src']
            if not message or not ref:
                transl_json_copy.remove(item)
                continue

            self.line_count += 1
            message = message.replace("\n", "").replace("\r", "")
            ref = ref.replace("\n", "").replace("\r", "")
            src = src.replace("\n", "").replace("\r", "")
            mt_list.append(message)
            ref_list.append(ref)
            src_list.append(src)

            if "chrf" in self.task:
                # 实例化ChrFCalculator
                calculator = ChrFCalculator()
                stats = calculator.get_chrf_statistics(message, ref, n=self.chrf_ngram)  # 获取统计数据而不是分数
                file_stats=calculator.add_stats(file_stats,stats)
                self.chrf_all_stats=calculator.add_stats(self.chrf_all_stats,stats)
                item['chrf_score'] = calculator.compute_f_score(stats,beta=self.chrf_beta)  # 单句的分数仍然计算

            if "cutoff_rate" in self.task:
                if len(mt)>len(src):
                    self.cutoff_count+=len(mt)-len(src)


        if "chrf" in self.task:
            # 使用累积的统计数据计算文件的总体分数
            self.chrf_file_stats[file_path] = file_stats
        
        if "comet" in self.task:
            if not self.comet_calculator:
                self.comet_calculator=comet_calculator()
            comet_scores = self.comet_calculator.get_comet_score_batch(src_list,mt_list, ref_list,self.comet_batch_size)
            for i, item in enumerate(transl_json_copy):
                comet_scores[i]=comet_scores[i]*100
                item['comet_score'] = comet_scores[i]
        
            avg_comet_score = sum([x["comet_score"] for x in transl_json_copy]) / len(comet_scores)
            self.comet_file_stats[file_path] = avg_comet_score

        
        with open(file_path, "wb") as f:
            f.write(orjson.dumps(transl_json, option=orjson.OPT_INDENT_2))

    def gtp_final(self):
        """
        This method is called after all translations are done.
        在所有文件翻译完成之后的动作，例如输出提示信息。
        """
        if "chrf" in self.task:
            # 实例化ChrFCalculator
            calculator = ChrFCalculator()
            # 使用累积的统计数据计算每个文件的分数
            for file_path, stats in self.chrf_file_stats.items():
                file_score = calculator.compute_f_score(stats)
                file_name=os.path.basename(file_path)
                LOGGER.info(f"[chrF翻译评估] {file_name} 的 chrF 分数为 {file_score:.2f}")
            
            # 使用所有统计数据计算总体分数
            total_avg_chrf_score = calculator.compute_f_score(self.chrf_all_stats)
            LOGGER.info(f"[chrF翻译评估] 总体 chrF 分数为 {total_avg_chrf_score:.2f}")
        
        if "comet" in self.task:
            for file_path, avg_comet_score in self.comet_file_stats.items():
                file_name=os.path.basename(file_path)
                LOGGER.info(f"[COMET翻译评估] {file_name} 的 COMET 分数为 {avg_comet_score:.2f}")
            
            # 总平均分
            total_avg_comet_score = sum(self.comet_file_stats.values()) / len(self.comet_file_stats)
            LOGGER.info(f"[COMET翻译评估] 总体 COMET 分数为 {total_avg_comet_score:.2f}")
        
        if "cutoff_rate" in self.task:
            cutoff_rate=self.cutoff_count/self.line_count * 1000
            LOGGER.info(f"[Cutoff翻译评估] 千句截断字符数为 {cutoff_rate:.2f}")



class ChrFCalculator:
    def get_chrf_statistics(self, input_str: str, ref: str, n: int = 6) -> list:
        """获取chrF统计数据"""
        stats = []
        for i in range(1, n + 1):
            hyp_ngrams = self.extract_char_ngrams(input_str, i)
            ref_ngrams = self.extract_char_ngrams(ref, i)
            stats.extend(self.get_match_statistics(hyp_ngrams, ref_ngrams))
        return stats

    def extract_char_ngrams(self, text: str, n: int) -> dict:
        # 移除空格并获取字符n-gram
        text = ''.join(text.split())
        ngrams = {}
        for i in range(len(text) - n + 1):
            gram = text[i:i+n]
            ngrams[gram] = ngrams.get(gram, 0) + 1
        return ngrams

    def get_match_statistics(self, hyp_ngrams: dict, ref_ngrams: dict) -> list:
        # 计算假设和参考n-gram之间的匹配
        match_count = 0
        hyp_count = sum(hyp_ngrams.values())
        ref_count = sum(ref_ngrams.values())
        
        for ng, count in hyp_ngrams.items():
            if ng in ref_ngrams:
                match_count += min(count, ref_ngrams[ng])
                
        return [hyp_count if ref_ngrams else 0, ref_count, match_count]

    def add_stats(self, stats_a, stats_b):
        result=[]
        if not stats_a:
            return stats_b
        if not stats_b:
            return stats_a
        for i in range(len(stats_a)):
            result.append(stats_a[i]+stats_b[i])
        return result
        
    def compute_f_score(self, statistics: list, beta: int = 2) -> float:
        """从匹配统计数据计算chrF分数
        
        Args:
            statistics: 每个n-gram顺序的 [hyp_count, ref_count, match_count] 列表
            beta: 召回率与精确率的权重 (默认: 2)
        
        Returns:
            chrF 分数，介于0和100之间
        """
        eps = 1e-16
        score = 0.0
        effective_order = 0
        factor = beta ** 2
        avg_prec, avg_rec = 0.0, 0.0

        # 处理每个n-gram顺序的统计数据
        n = len(statistics) // 3
        for i in range(n):
            n_hyp, n_ref, n_match = statistics[3 * i: 3 * i + 3]
            
            prec = n_match / n_hyp if n_hyp > 0 else eps
            rec = n_match / n_ref if n_ref > 0 else eps

            if n_hyp > 0 and n_ref > 0:
                avg_prec += prec
                avg_rec += rec
                effective_order += 1

        if effective_order == 0:
            return 0.0
            
        avg_prec /= effective_order
        avg_rec /= effective_order

        if avg_prec + avg_rec:
            score = (1 + factor) * avg_prec * avg_rec
            score /= ((factor * avg_prec) + avg_rec)
            return 100 * score
        return 0.0

class comet_calculator:
    def __init__(self):
        from comet import download_model, load_from_checkpoint

        model_path = download_model("Unbabel/wmt22-comet-da")
        model = load_from_checkpoint(model_path)

        self.model = model
    
    def get_comet_score_batch(self,src_list,mt_list: list, ref_list: list,batch_size:int) -> list:
        if len(src_list) != len(mt_list) or len(src_list) != len(ref_list):
            raise ValueError("输入列表长度不一致")
        

        data = []
        scores=[]
        for i in range(len(src_list)):
            data.append({
                "src": src_list[i],
                "mt": mt_list[i],
                "ref": ref_list[i]
            })

        model_output = self.model.predict(data, batch_size=batch_size, gpus=1)
        scores=model_output.scores
        return scores


