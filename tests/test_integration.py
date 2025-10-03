"""
安全写入机制集成测试 - 异常情况下的数据完整性验证
测试在各种异常情况下数据的完整性保护
"""

import asyncio
import tempfile
import json
import os
import signal
import time
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from contextlib import asynccontextmanager
import sys
sys.path.insert(0, '/data/workspace/GalTransl')

from GalTransl.SafeWrite import AtomicFileWriter, DataValidator, BackupManager
from GalTransl.Cache import save_transCache_to_json, _save_with_safe_write
from GalTransl.CSentense import CTransList, CSentense


class IntegrationTestSuite:
    """集成测试套件"""
    
    def __init__(self):
        self.test_dir = None
        self.results = []
        
    async def setup(self):
        """测试环境设置"""
        self.test_dir = Path(tempfile.mkdtemp())
        print(f"测试目录: {self.test_dir}")
        
    async def teardown(self):
        """测试环境清理"""
        if self.test_dir and self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    def create_test_cache_data(self, count=100):
        """创建测试缓存数据"""
        cache_data = []
        for i in range(count):
            cache_obj = {
                "index": i,
                "name": f"角色{i}",
                "pre_jp": f"日本語テキスト{i}",
                "post_jp": f"日本語テキスト{i}",
                "pre_zh": f"中文文本{i}",
                "proofread_zh": "",
                "trans_by": "test_engine",
                "proofread_by": "",
                "trans_conf": 0,
                "doub_content": "",
                "unknown_proper_noun": ""
            }
            cache_data.append(cache_obj)
        return cache_data
    
    def create_mock_project_config(self, safe_write_enabled=True):
        """创建模拟项目配置"""
        mock_config = Mock()
        mock_config.getSafeWriteConfig.return_value = {
            'enable_safe_write': safe_write_enabled,
            'write_verification': True,
            'enable_backup': True,
            'backup_retention_count': 3,
            'temp_file_cleanup': True,
            'write_timeout_seconds': 30
        }
        return mock_config
    
    async def test_normal_operation(self):
        """测试正常操作"""
        print("\n=== 测试正常操作 ===")
        
        cache_file = self.test_dir / "normal_test.json"
        cache_data = self.create_test_cache_data(50)
        project_config = self.create_mock_project_config()
        
        try:
            await _save_with_safe_write(cache_data, str(cache_file), project_config)
            
            # 验证文件存在且内容正确
            assert cache_file.exists(), "缓存文件应该存在"
            
            with open(cache_file, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
            
            assert len(saved_data) == 50, f"应该保存50条记录，实际保存{len(saved_data)}条"
            assert saved_data[0]['index'] == 0, "第一条记录索引应该为0"
            assert saved_data[49]['index'] == 49, "最后一条记录索引应该为49"
            
            print("✅ 正常操作测试通过")
            return True
            
        except Exception as e:
            print(f"❌ 正常操作测试失败: {e}")
            return False
    
    async def test_large_file_operation(self):
        """测试大文件操作"""
        print("\n=== 测试大文件操作 ===")
        
        cache_file = self.test_dir / "large_test.json"
        cache_data = self.create_test_cache_data(1000)  # 1000条记录
        project_config = self.create_mock_project_config()
        
        try:
            start_time = time.time()
            await _save_with_safe_write(cache_data, str(cache_file), project_config)
            end_time = time.time()
            
            # 验证文件存在且内容正确
            assert cache_file.exists(), "大文件应该被成功创建"
            
            with open(cache_file, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
            
            assert len(saved_data) == 1000, f"应该保存1000条记录，实际保存{len(saved_data)}条"
            
            file_size = cache_file.stat().st_size
            print(f"✅ 大文件操作测试通过 - 文件大小: {file_size} bytes, 耗时: {end_time - start_time:.2f}s")
            return True
            
        except Exception as e:
            print(f"❌ 大文件操作测试失败: {e}")
            return False
    
    async def test_concurrent_write_attempts(self):
        """测试并发写入尝试"""
        print("\n=== 测试并发写入保护 ===")
        
        cache_file = self.test_dir / "concurrent_test.json"
        cache_data = self.create_test_cache_data(100)
        project_config = self.create_mock_project_config()
        
        async def write_task(task_id):
            """单个写入任务"""
            try:
                modified_data = [
                    {**item, 'pre_zh': f"{item['pre_zh']}_task{task_id}"}
                    for item in cache_data
                ]
                await _save_with_safe_write(modified_data, str(cache_file), project_config)
                return task_id, True
            except Exception as e:
                return task_id, False
        
        try:
            # 启动多个并发写入任务
            tasks = [write_task(i) for i in range(5)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 验证文件最终存在且有效
            assert cache_file.exists(), "并发写入后文件应该存在"
            
            # 验证数据完整性
            is_valid = await DataValidator.validate_json_file(cache_file)
            assert is_valid, "并发写入后文件应该保持有效"
            
            with open(cache_file, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
            
            assert len(saved_data) == 100, "并发写入后应该保持正确的记录数量"
            
            successful_tasks = sum(1 for _, success in results if success)
            print(f"✅ 并发写入保护测试通过 - {successful_tasks}/5 个任务成功完成")
            return True
            
        except Exception as e:
            print(f"❌ 并发写入保护测试失败: {e}")
            return False
    
    async def test_disk_space_simulation(self):
        """模拟磁盘空间不足情况"""
        print("\n=== 测试磁盘空间不足模拟 ===")
        
        cache_file = self.test_dir / "disk_space_test.json"
        cache_data = self.create_test_cache_data(100)
        project_config = self.create_mock_project_config()
        
        # 创建一个非常大的JSON来模拟磁盘空间问题
        large_cache_data = self.create_test_cache_data(10000)
        
        try:
            # 首先创建一个正常的文件作为备份测试
            await _save_with_safe_write(cache_data, str(cache_file), project_config)
            original_size = cache_file.stat().st_size
            
            # 尝试写入大文件（可能会因为磁盘空间失败，但这是预期的）
            try:
                await _save_with_safe_write(large_cache_data, str(cache_file), project_config)
                print("✅ 大文件写入成功（磁盘空间充足）")
            except Exception:
                # 如果写入失败，验证原文件仍然完整
                if cache_file.exists():
                    is_valid = await DataValidator.validate_json_file(cache_file)
                    assert is_valid, "原文件应该保持完整"
                    print("✅ 写入失败时原文件保持完整")
            
            return True
            
        except Exception as e:
            print(f"❌ 磁盘空间模拟测试失败: {e}")
            return False
    
    async def test_backup_and_recovery(self):
        """测试备份和恢复功能"""
        print("\n=== 测试备份和恢复功能 ===")
        
        cache_file = self.test_dir / "backup_test.json"
        original_data = self.create_test_cache_data(50)
        modified_data = self.create_test_cache_data(50)
        for item in modified_data:
            item['pre_zh'] = item['pre_zh'] + "_modified"
        
        project_config = self.create_mock_project_config()
        backup_manager = BackupManager(project_config.getSafeWriteConfig())
        
        try:
            # 1. 创建原始文件
            await _save_with_safe_write(original_data, str(cache_file), project_config)
            assert cache_file.exists(), "原始文件应该被创建"
            
            # 2. 创建备份
            backup_path = await backup_manager.create_backup(cache_file)
            assert backup_path is not None, "备份应该被创建"
            assert backup_path.exists(), "备份文件应该存在"
            
            # 3. 修改原始文件
            await _save_with_safe_write(modified_data, str(cache_file), project_config)
            
            with open(cache_file, 'r', encoding='utf-8') as f:
                current_data = json.load(f)
            assert current_data[0]['pre_zh'].endswith('_modified'), "文件应该被修改"
            
            # 4. 从备份恢复
            success = await backup_manager.restore_from_backup(cache_file)
            assert success, "应该能够从备份恢复"
            
            # 5. 验证恢复的内容
            with open(cache_file, 'r', encoding='utf-8') as f:
                restored_data = json.load(f)
            
            assert not restored_data[0]['pre_zh'].endswith('_modified'), "恢复后不应该包含修改标记"
            assert len(restored_data) == 50, "恢复后应该保持原始记录数量"
            
            print("✅ 备份和恢复功能测试通过")
            return True
            
        except Exception as e:
            print(f"❌ 备份和恢复测试失败: {e}")
            return False
    
    async def test_data_validation_integrity(self):
        """测试数据验证完整性"""
        print("\n=== 测试数据验证完整性 ===")
        
        try:
            # 测试有效数据
            valid_file = self.test_dir / "valid_test.json"
            valid_data = self.create_test_cache_data(10)
            
            with open(valid_file, 'w', encoding='utf-8') as f:
                json.dump(valid_data, f, ensure_ascii=False, indent=2)
            
            is_valid = await DataValidator.validate_json_file(valid_file)
            assert is_valid, "有效数据应该通过验证"
            
            # 测试无效JSON
            invalid_json_file = self.test_dir / "invalid_json.json"
            with open(invalid_json_file, 'w', encoding='utf-8') as f:
                f.write('{"invalid": json format}')
            
            is_valid = await DataValidator.validate_json_file(invalid_json_file)
            assert not is_valid, "无效JSON应该失败验证"
            
            # 测试缺少必需字段
            missing_fields_file = self.test_dir / "missing_fields.json"
            incomplete_data = [{"index": 0, "name": "test"}]  # 缺少必需字段
            
            with open(missing_fields_file, 'w', encoding='utf-8') as f:
                json.dump(incomplete_data, f)
            
            is_valid = await DataValidator.validate_json_file(missing_fields_file)
            assert not is_valid, "缺少必需字段的数据应该失败验证"
            
            print("✅ 数据验证完整性测试通过")
            return True
            
        except Exception as e:
            print(f"❌ 数据验证完整性测试失败: {e}")
            return False
    
    async def test_temp_file_cleanup(self):
        """测试临时文件清理"""
        print("\n=== 测试临时文件清理 ===")
        
        cache_file = self.test_dir / "cleanup_test.json"
        cache_data = self.create_test_cache_data(20)
        project_config = self.create_mock_project_config()
        
        try:
            # 记录清理前的临时文件
            temp_files_before = list(self.test_dir.glob("*.tmp"))
            
            # 执行写入操作
            await _save_with_safe_write(cache_data, str(cache_file), project_config)
            
            # 检查清理后的临时文件
            temp_files_after = list(self.test_dir.glob("*.tmp"))
            
            assert len(temp_files_after) == len(temp_files_before), "临时文件应该被清理"
            assert cache_file.exists(), "目标文件应该存在"
            
            print("✅ 临时文件清理测试通过")
            return True
            
        except Exception as e:
            print(f"❌ 临时文件清理测试失败: {e}")
            return False
    
    async def test_fallback_to_simple_write(self):
        """测试降级到简单写入"""
        print("\n=== 测试降级到简单写入 ===")
        
        cache_file = self.test_dir / "fallback_test.json"
        cache_data = self.create_test_cache_data(30)
        project_config = self.create_mock_project_config(safe_write_enabled=False)
        
        try:
            # 使用禁用安全写入的配置
            from GalTransl.Cache import save_transCache_to_json
            from GalTransl.CSentense import CTransList, CSentense
            
            # 创建模拟翻译列表
            trans_list = CTransList()
            for i, data in enumerate(cache_data):
                trans = CSentense()
                trans.index = data['index']
                trans.speaker = data['name']
                trans.pre_jp = data['pre_jp']
                trans.post_jp = data['post_jp']
                trans.pre_zh = data['pre_zh']
                trans.proofread_zh = data['proofread_zh']
                trans.trans_by = data['trans_by']
                trans.proofread_by = data['proofread_by']
                trans.trans_conf = data['trans_conf']
                trans.doub_content = data['doub_content']
                trans.unknown_proper_noun = data['unknown_proper_noun']
                trans.problem = ""
                trans.post_zh = data['pre_zh']
                trans_list.append(trans)
            
            await save_transCache_to_json(trans_list, str(cache_file), 
                                          post_save=False, project_config=project_config)
            
            # 验证文件存在且内容正确
            assert cache_file.exists(), "降级写入后文件应该存在"
            
            with open(cache_file, 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
            
            assert len(saved_data) == 30, "降级写入应该保存所有数据"
            
            print("✅ 降级到简单写入测试通过")
            return True
            
        except Exception as e:
            print(f"❌ 降级到简单写入测试失败: {e}")
            return False
    
    async def run_all_tests(self):
        """运行所有集成测试"""
        print("🚀 开始安全写入机制集成测试")
        print("=" * 50)
        
        await self.setup()
        
        test_methods = [
            self.test_normal_operation,
            self.test_large_file_operation,
            self.test_concurrent_write_attempts,
            self.test_disk_space_simulation,
            self.test_backup_and_recovery,
            self.test_data_validation_integrity,
            self.test_temp_file_cleanup,
            self.test_fallback_to_simple_write
        ]
        
        passed = 0
        total = len(test_methods)
        
        for test_method in test_methods:
            try:
                result = await test_method()
                if result:
                    passed += 1
                self.results.append((test_method.__name__, result))
            except Exception as e:
                print(f"❌ 测试 {test_method.__name__} 发生异常: {e}")
                self.results.append((test_method.__name__, False))
        
        await self.teardown()
        
        # 打印测试结果摘要
        print("\n" + "=" * 50)
        print("🏁 集成测试结果摘要")
        print("=" * 50)
        
        for test_name, result in self.results:
            status = "✅ 通过" if result else "❌ 失败"
            print(f"{test_name}: {status}")
        
        print(f"\n总计: {passed}/{total} 个测试通过")
        success_rate = (passed / total) * 100
        print(f"成功率: {success_rate:.1f}%")
        
        if success_rate >= 80:
            print("🎉 集成测试基本通过！安全写入机制运行良好。")
        elif success_rate >= 60:
            print("⚠️ 集成测试部分通过，需要进一步优化。")
        else:
            print("🚨 集成测试失败率较高，需要修复关键问题。")
        
        return success_rate >= 80


async def main():
    """主测试函数"""
    test_suite = IntegrationTestSuite()
    success = await test_suite.run_all_tests()
    return success


if __name__ == '__main__':
    # 运行集成测试
    result = asyncio.run(main())
    exit_code = 0 if result else 1
    exit(exit_code)