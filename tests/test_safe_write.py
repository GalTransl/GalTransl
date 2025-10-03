"""
安全写入机制单元测试
"""

import unittest
import asyncio
import tempfile
import json
import os
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
import sys
sys.path.insert(0, '/data/workspace/GalTransl')

from GalTransl.SafeWrite import AtomicFileWriter, DataValidator, BackupManager, SafeWriteConfig
from GalTransl.Cache import save_transCache_to_json, _save_with_safe_write, _save_with_simple_write
from GalTransl.CSentense import CTransList, CSentense


class TestAtomicFileWriter(unittest.IsolatedAsyncioTestCase):
    """原子文件写入器测试"""
    
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.test_file = self.test_dir / "test_cache.json"
        
    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    async def test_atomic_write_success(self):
        """测试正常的原子写入操作"""
        test_data = b'{"test": "data"}'
        
        async with AtomicFileWriter(str(self.test_file)) as writer:
            success = await writer.write_atomic(test_data)
            self.assertTrue(success)
        
        # 验证文件存在且内容正确
        self.assertTrue(self.test_file.exists())
        with open(self.test_file, 'rb') as f:
            content = f.read()
        self.assertEqual(content, test_data)
    
    async def test_atomic_write_with_validation(self):
        """测试带验证的原子写入操作"""
        test_data = b'{"test": "data"}'
        
        async def mock_validate(file_path):
            return True
        
        async with AtomicFileWriter(str(self.test_file)) as writer:
            success = await writer.write_atomic(test_data, mock_validate)
            self.assertTrue(success)
    
    async def test_atomic_write_validation_failure(self):
        """测试验证失败的情况"""
        test_data = b'{"test": "data"}'
        
        async def mock_validate(file_path):
            return False
        
        async with AtomicFileWriter(str(self.test_file)) as writer:
            success = await writer.write_atomic(test_data, mock_validate)
            self.assertFalse(success)
        
        # 验证目标文件不存在
        self.assertFalse(self.test_file.exists())
    
    async def test_temp_file_cleanup(self):
        """测试临时文件清理"""
        test_data = b'{"test": "data"}'
        temp_files_before = list(self.test_dir.glob("*.tmp"))
        
        async with AtomicFileWriter(str(self.test_file)) as writer:
            await writer.write_atomic(test_data)
        
        # 验证临时文件被清理
        temp_files_after = list(self.test_dir.glob("*.tmp"))
        self.assertEqual(len(temp_files_before), len(temp_files_after))


class TestDataValidator(unittest.IsolatedAsyncioTestCase):
    """数据验证器测试"""
    
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        
    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    async def test_validate_valid_json(self):
        """测试验证有效的JSON文件"""
        test_file = self.test_dir / "valid.json"
        test_data = [
            {
                "index": 0,
                "name": "speaker",
                "pre_jp": "日本語",
                "post_jp": "日本語",
                "pre_zh": "中文"
            }
        ]
        
        with open(test_file, 'w', encoding='utf-8') as f:
            json.dump(test_data, f)
        
        result = await DataValidator.validate_json_file(test_file)
        self.assertTrue(result)
    
    async def test_validate_invalid_json(self):
        """测试验证无效的JSON文件"""
        test_file = self.test_dir / "invalid.json"
        
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write('{"invalid": json}')
        
        result = await DataValidator.validate_json_file(test_file)
        self.assertFalse(result)
    
    async def test_validate_empty_file(self):
        """测试验证空文件"""
        test_file = self.test_dir / "empty.json"
        test_file.touch()
        
        result = await DataValidator.validate_json_file(test_file)
        self.assertFalse(result)
    
    async def test_validate_missing_required_fields(self):
        """测试验证缺少必需字段的文件"""
        test_file = self.test_dir / "missing_fields.json"
        test_data = [
            {
                "index": 0,
                "name": "speaker"
                # 缺少 pre_jp, post_jp, pre_zh
            }
        ]
        
        with open(test_file, 'w', encoding='utf-8') as f:
            json.dump(test_data, f)
        
        result = await DataValidator.validate_json_file(test_file)
        self.assertFalse(result)


class TestBackupManager(unittest.IsolatedAsyncioTestCase):
    """备份管理器测试"""
    
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.test_file = self.test_dir / "test.json"
        self.backup_manager = BackupManager({'backup_retention_count': 2})
        
    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    async def test_create_backup(self):
        """测试创建备份"""
        # 创建原始文件
        test_data = '{"test": "data"}'
        with open(self.test_file, 'w') as f:
            f.write(test_data)
        
        # 创建备份
        backup_path = await self.backup_manager.create_backup(self.test_file)
        
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.exists())
        
        # 验证备份内容
        with open(backup_path, 'r') as f:
            backup_content = f.read()
        self.assertEqual(backup_content, test_data)
    
    async def test_backup_retention(self):
        """测试备份保留策略"""
        # 创建原始文件
        with open(self.test_file, 'w') as f:
            f.write('{"test": "data"}')
        
        # 创建多个备份
        backups = []
        for i in range(4):
            await asyncio.sleep(0.01)  # 确保时间戳不同
            backup_path = await self.backup_manager.create_backup(self.test_file)
            backups.append(backup_path)
        
        # 验证只保留指定数量的备份
        existing_backups = list(self.test_dir.glob("*_backup.json"))
        self.assertEqual(len(existing_backups), 2)
    
    async def test_restore_from_backup(self):
        """测试从备份恢复"""
        # 创建原始文件和备份
        original_data = '{"test": "original"}'
        with open(self.test_file, 'w') as f:
            f.write(original_data)
        
        backup_path = await self.backup_manager.create_backup(self.test_file)
        
        # 修改原始文件
        modified_data = '{"test": "modified"}'
        with open(self.test_file, 'w') as f:
            f.write(modified_data)
        
        # 从备份恢复
        success = await self.backup_manager.restore_from_backup(self.test_file)
        self.assertTrue(success)
        
        # 验证内容恢复
        with open(self.test_file, 'r') as f:
            restored_content = f.read()
        self.assertEqual(restored_content, original_data)


class TestSafeWriteConfig(unittest.TestCase):
    """安全写入配置测试"""
    
    def test_default_config(self):
        """测试默认配置"""
        config = SafeWriteConfig()
        
        self.assertTrue(config.is_enabled())
        self.assertEqual(config.get('backup_retention_count'), 3)
        self.assertTrue(config.get('write_verification'))
        self.assertTrue(config.get('enable_backup'))
    
    def test_custom_config(self):
        """测试自定义配置"""
        custom_config = {
            'enable_safe_write': False,
            'backup_retention_count': 5,
            'write_verification': False
        }
        
        config = SafeWriteConfig(custom_config)
        
        self.assertFalse(config.is_enabled())
        self.assertEqual(config.get('backup_retention_count'), 5)
        self.assertFalse(config.get('write_verification'))


class TestSafeWriteIntegration(unittest.IsolatedAsyncioTestCase):
    """安全写入集成测试"""
    
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        
    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    async def test_save_with_safe_write(self):
        """测试安全写入保存"""
        cache_file_path = str(self.test_dir / "cache.json")
        cache_json = [
            {
                "index": 0,
                "name": "speaker",
                "pre_jp": "日本語",
                "post_jp": "日本語",
                "pre_zh": "中文",
                "proofread_zh": "",
                "trans_by": "test",
                "proofread_by": ""
            }
        ]
        
        # 模拟项目配置
        mock_config = Mock()
        mock_config.getSafeWriteConfig.return_value = {
            'enable_safe_write': True,
            'write_verification': True,
            'enable_backup': True,
            'backup_retention_count': 3
        }
        
        await _save_with_safe_write(cache_json, cache_file_path, mock_config)
        
        # 验证文件存在且内容正确
        self.assertTrue(Path(cache_file_path).exists())
        
        with open(cache_file_path, 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        
        self.assertEqual(len(saved_data), 1)
        self.assertEqual(saved_data[0]['index'], 0)
        self.assertEqual(saved_data[0]['pre_zh'], '中文')
    
    async def test_save_with_simple_write(self):
        """测试简单写入保存"""
        cache_file_path = str(self.test_dir / "cache.json")
        cache_json = [
            {
                "index": 0,
                "name": "speaker",
                "pre_jp": "日本語",
                "post_jp": "日本語",
                "pre_zh": "中文",
                "proofread_zh": "",
                "trans_by": "test",
                "proofread_by": ""
            }
        ]
        
        await _save_with_simple_write(cache_json, cache_file_path)
        
        # 验证文件存在且内容正确
        self.assertTrue(Path(cache_file_path).exists())
        
        with open(cache_file_path, 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        
        self.assertEqual(len(saved_data), 1)
        self.assertEqual(saved_data[0]['pre_zh'], '中文')


def create_mock_trans_list():
    """创建模拟的翻译列表"""
    trans_list = CTransList()
    
    # 创建一个测试翻译对象
    trans = CSentense()
    trans.index = 0
    trans.speaker = "测试角色"
    trans.pre_jp = "こんにちは"
    trans.post_jp = "こんにちは"
    trans.pre_zh = "你好"
    trans.proofread_zh = ""
    trans.trans_by = "test_engine"
    trans.proofread_by = ""
    trans.trans_conf = 0
    trans.doub_content = ""
    trans.unknown_proper_noun = ""
    trans.problem = ""
    trans.post_zh = "你好"
    
    trans_list.append(trans)
    return trans_list


class TestCacheIntegration(unittest.IsolatedAsyncioTestCase):
    """缓存集成测试"""
    
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        
    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
    
    async def test_save_transCache_to_json_with_safe_write(self):
        """测试集成的缓存保存功能（安全写入模式）"""
        cache_file_path = str(self.test_dir / "test_cache")
        trans_list = create_mock_trans_list()
        
        # 模拟项目配置
        mock_config = Mock()
        mock_config.getSafeWriteConfig.return_value = {
            'enable_safe_write': True,
            'write_verification': True,
            'enable_backup': True,
            'backup_retention_count': 3
        }
        
        await save_transCache_to_json(trans_list, cache_file_path, 
                                      post_save=True, project_config=mock_config)
        
        # 验证文件存在
        expected_file = Path(cache_file_path + ".json")
        self.assertTrue(expected_file.exists())
        
        # 验证内容
        with open(expected_file, 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        
        self.assertEqual(len(saved_data), 1)
        self.assertEqual(saved_data[0]['speaker'], "测试角色")
        self.assertEqual(saved_data[0]['pre_zh'], "你好")
    
    async def test_save_transCache_to_json_with_simple_write(self):
        """测试集成的缓存保存功能（简单写入模式）"""
        cache_file_path = str(self.test_dir / "test_cache")
        trans_list = create_mock_trans_list()
        
        # 模拟项目配置（禁用安全写入）
        mock_config = Mock()
        mock_config.getSafeWriteConfig.return_value = {
            'enable_safe_write': False
        }
        
        await save_transCache_to_json(trans_list, cache_file_path, 
                                      post_save=False, project_config=mock_config)
        
        # 验证文件存在
        expected_file = Path(cache_file_path + ".json")
        self.assertTrue(expected_file.exists())
        
        # 验证内容
        with open(expected_file, 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        
        self.assertEqual(len(saved_data), 1)
        self.assertEqual(saved_data[0]['speaker'], "测试角色")


if __name__ == '__main__':
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # 添加所有测试类
    suite.addTests(loader.loadTestsFromTestCase(TestAtomicFileWriter))
    suite.addTests(loader.loadTestsFromTestCase(TestDataValidator))
    suite.addTests(loader.loadTestsFromTestCase(TestBackupManager))
    suite.addTests(loader.loadTestsFromTestCase(TestSafeWriteConfig))
    suite.addTests(loader.loadTestsFromTestCase(TestSafeWriteIntegration))
    suite.addTests(loader.loadTestsFromTestCase(TestCacheIntegration))
    
    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # 打印结果摘要
    print(f"\n测试总数: {result.testsRun}")
    print(f"失败: {len(result.failures)}")
    print(f"错误: {len(result.errors)}")
    print(f"成功率: {((result.testsRun - len(result.failures) - len(result.errors)) / result.testsRun * 100):.1f}%")