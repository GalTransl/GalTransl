"""
安全写入机制模块
实现原子写入、数据验证和备份管理功能
"""

import os
import time
import tempfile
import hashlib
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, Union
from datetime import datetime

import orjson
import aiofiles
from GalTransl import LOGGER
from GalTransl.i18n import get_text, GT_LANG


class AtomicFileWriter:
    """
    原子文件写入器
    
    使用临时文件 + 原子替换策略确保写入操作的原子性
    防止程序异常中断导致的数据损坏
    """
    
    def __init__(self, target_path: str, backup_manager: Optional['BackupManager'] = None):
        """
        初始化原子文件写入器
        
        Args:
            target_path: 目标文件路径
            backup_manager: 备份管理器实例
        """
        self.target_path = Path(target_path)
        self.backup_manager = backup_manager
        self.temp_path = None
        self.temp_file = None
        self._lock = asyncio.Lock()
        
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._lock.acquire()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        try:
            # 清理临时文件
            if self.temp_path and self.temp_path.exists():
                try:
                    self.temp_path.unlink()
                    LOGGER.debug(f"[SafeWrite] 已清理临时文件: {self.temp_path}")
                except Exception as e:
                    LOGGER.warning(f"[SafeWrite] 清理临时文件失败: {e}")
        finally:
            self._lock.release()
    
    async def write_atomic(self, data: bytes, validate_func: Optional[callable] = None) -> bool:
        """
        原子写入数据到目标文件
        
        Args:
            data: 要写入的数据
            validate_func: 可选的验证函数，用于验证写入的数据
            
        Returns:
            bool: 写入是否成功
        """
        try:
            # 1. 创建临时文件
            await self._create_temp_file()
            
            # 2. 写入数据到临时文件
            await self._write_to_temp(data)
            
            # 3. 验证数据完整性
            if validate_func and not await validate_func(self.temp_path):
                LOGGER.error(f"[SafeWrite] 数据验证失败: {self.target_path}")
                return False
            
            # 4. 备份现有文件（如果存在）
            if self.backup_manager and self.target_path.exists():
                await self.backup_manager.create_backup(self.target_path)
            
            # 5. 原子替换目标文件
            await self._atomic_replace()
            
            LOGGER.debug(f"[SafeWrite] 原子写入成功: {self.target_path}")
            return True
            
        except Exception as e:
            LOGGER.error(f"[SafeWrite] 原子写入失败: {self.target_path}, 错误: {e}")
            return False
    
    async def _create_temp_file(self):
        """创建临时文件"""
        # 确保目标目录存在
        self.target_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 在目标文件的同一目录创建临时文件
        temp_dir = self.target_path.parent
        suffix = self.target_path.suffix
        
        # 创建唯一的临时文件名
        timestamp = int(time.time() * 1000000)  # 微秒级时间戳
        temp_name = f".{self.target_path.stem}_{timestamp}_tmp{suffix}"
        self.temp_path = temp_dir / temp_name
        
        LOGGER.debug(f"[SafeWrite] 创建临时文件: {self.temp_path}")
    
    async def _write_to_temp(self, data: bytes):
        """写入数据到临时文件"""
        try:
            async with aiofiles.open(self.temp_path, 'wb') as f:
                await f.write(data)
                await f.fsync()  # 强制同步到磁盘
            
            LOGGER.debug(f"[SafeWrite] 数据已写入临时文件: {len(data)} bytes")
            
        except Exception as e:
            LOGGER.error(f"[SafeWrite] 写入临时文件失败: {e}")
            raise
    
    async def _atomic_replace(self):
        """原子替换目标文件"""
        try:
            # 在同一文件系统内的移动操作是原子的
            self.temp_path.replace(self.target_path)
            LOGGER.debug(f"[SafeWrite] 原子替换完成: {self.target_path}")
            
        except Exception as e:
            LOGGER.error(f"[SafeWrite] 原子替换失败: {e}")
            raise


class DataValidator:
    """
    数据验证器
    
    验证JSON格式、数据完整性和结构正确性
    """
    
    @staticmethod
    async def validate_json_file(file_path: Path) -> bool:
        """
        验证JSON文件格式和结构
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 验证是否通过
        """
        try:
            # 1. 检查文件是否存在且非空
            if not file_path.exists():
                LOGGER.error(f"[DataValidator] 文件不存在: {file_path}")
                return False
                
            file_size = file_path.stat().st_size
            if file_size == 0:
                LOGGER.error(f"[DataValidator] 文件为空: {file_path}")
                return False
            
            # 2. 验证JSON格式
            async with aiofiles.open(file_path, 'rb') as f:
                content = await f.read()
                
            try:
                data = orjson.loads(content)
            except orjson.JSONDecodeError as e:
                LOGGER.error(f"[DataValidator] JSON格式错误: {e}")
                return False
            
            # 3. 验证数据结构
            if not await DataValidator._validate_cache_structure(data):
                return False
            
            LOGGER.debug(f"[DataValidator] 文件验证通过: {file_path}")
            return True
            
        except Exception as e:
            LOGGER.error(f"[DataValidator] 文件验证异常: {e}")
            return False
    
    @staticmethod
    async def _validate_cache_structure(data: Any) -> bool:
        """
        验证缓存数据结构
        
        Args:
            data: JSON数据
            
        Returns:
            bool: 结构是否正确
        """
        try:
            if not isinstance(data, list):
                LOGGER.error("[DataValidator] 缓存数据应为列表结构")
                return False
            
            # 验证关键字段
            required_fields = {"index", "name", "pre_jp", "post_jp", "pre_zh"}
            
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    LOGGER.error(f"[DataValidator] 第{i}项不是字典结构")
                    return False
                
                # 检查必需字段
                missing_fields = required_fields - set(item.keys())
                if missing_fields:
                    LOGGER.error(f"[DataValidator] 第{i}项缺少字段: {missing_fields}")
                    return False
                
                # 检查字段类型
                for field in required_fields:
                    if not isinstance(item[field], (str, int)):
                        LOGGER.error(f"[DataValidator] 第{i}项字段{field}类型错误")
                        return False
            
            LOGGER.debug(f"[DataValidator] 数据结构验证通过，共{len(data)}条记录")
            return True
            
        except Exception as e:
            LOGGER.error(f"[DataValidator] 数据结构验证异常: {e}")
            return False


class BackupManager:
    """
    备份管理器
    
    实现自动备份、恢复和备份文件管理功能
    """
    
    def __init__(self, backup_config: Optional[Dict[str, Any]] = None):
        """
        初始化备份管理器
        
        Args:
            backup_config: 备份配置
        """
        self.config = backup_config or {}
        self.retention_count = self.config.get('backup_retention_count', 3)
        self.enable_backup = self.config.get('enable_backup', True)
    
    async def create_backup(self, file_path: Path) -> Optional[Path]:
        """
        创建文件备份
        
        Args:
            file_path: 要备份的文件路径
            
        Returns:
            备份文件路径，失败时返回None
        """
        if not self.enable_backup:
            return None
            
        try:
            if not file_path.exists():
                LOGGER.debug(f"[BackupManager] 文件不存在，跳过备份: {file_path}")
                return None
            
            # 生成备份文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{file_path.stem}_{timestamp}_backup{file_path.suffix}"
            backup_path = file_path.parent / backup_name
            
            # 复制文件到备份位置
            async with aiofiles.open(file_path, 'rb') as src:
                content = await src.read()
            
            async with aiofiles.open(backup_path, 'wb') as dst:
                await dst.write(content)
            
            LOGGER.debug(f"[BackupManager] 备份创建成功: {backup_path}")
            
            # 清理过期备份
            await self._cleanup_old_backups(file_path)
            
            return backup_path
            
        except Exception as e:
            LOGGER.error(f"[BackupManager] 备份创建失败: {e}")
            return None
    
    async def restore_from_backup(self, file_path: Path) -> bool:
        """
        从备份恢复文件
        
        Args:
            file_path: 要恢复的文件路径
            
        Returns:
            bool: 恢复是否成功
        """
        try:
            # 查找最新的备份文件
            backup_pattern = f"{file_path.stem}_*_backup{file_path.suffix}"
            backup_files = list(file_path.parent.glob(backup_pattern))
            
            if not backup_files:
                LOGGER.warning(f"[BackupManager] 未找到备份文件: {file_path}")
                return False
            
            # 按时间排序，选择最新的备份
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            latest_backup = backup_files[0]
            
            # 验证备份文件
            if not await DataValidator.validate_json_file(latest_backup):
                LOGGER.error(f"[BackupManager] 备份文件验证失败: {latest_backup}")
                return False
            
            # 恢复文件
            async with aiofiles.open(latest_backup, 'rb') as src:
                content = await src.read()
            
            async with aiofiles.open(file_path, 'wb') as dst:
                await dst.write(content)
            
            LOGGER.info(f"[BackupManager] 文件恢复成功: {file_path} <- {latest_backup}")
            return True
            
        except Exception as e:
            LOGGER.error(f"[BackupManager] 文件恢复失败: {e}")
            return False
    
    async def _cleanup_old_backups(self, file_path: Path):
        """清理过期的备份文件"""
        try:
            backup_pattern = f"{file_path.stem}_*_backup{file_path.suffix}"
            backup_files = list(file_path.parent.glob(backup_pattern))
            
            if len(backup_files) <= self.retention_count:
                return
            
            # 按时间排序，删除最旧的备份
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            files_to_delete = backup_files[self.retention_count:]
            
            for backup_file in files_to_delete:
                backup_file.unlink()
                LOGGER.debug(f"[BackupManager] 删除过期备份: {backup_file}")
                
        except Exception as e:
            LOGGER.warning(f"[BackupManager] 清理备份文件失败: {e}")


class SafeWriteConfig:
    """
    安全写入配置管理
    """
    
    DEFAULT_CONFIG = {
        'enable_safe_write': True,
        'backup_retention_count': 3,
        'write_verification': True,
        'temp_file_cleanup': True,
        'write_timeout_seconds': 30,
        'enable_backup': True
    }
    
    def __init__(self, config_dict: Optional[Dict[str, Any]] = None):
        """
        初始化配置
        
        Args:
            config_dict: 配置字典
        """
        self.config = {**self.DEFAULT_CONFIG}
        if config_dict:
            self.config.update(config_dict)
    
    def get(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def is_enabled(self) -> bool:
        """检查安全写入是否启用"""
        return self.config.get('enable_safe_write', True)