"""
上下文记忆系统

维护 Agent 的对话历史、工具使用记录和项目修改历史，
帮助 Agent 在多轮对话中保持上下文。

功能：
- 记录用户输入和 AI 回复
- 跟踪工具使用情况
- 记录文件修改
- 生成记忆摘要
"""

from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from ..i18n import tr


@dataclass
class Interaction:
    """
    单轮交互记录
    
    包含用户输入、AI 回复和使用的工具。
    """
    timestamp: str
    user_input: str
    ai_response: str
    tools_used: List[str] = field(default_factory=list)
    tool_results: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    session_id: str = ""


@dataclass
class FileModification:
    """
    文件修改记录
    """
    timestamp: str
    file_path: str
    action: str  # created, modified, deleted
    details: str = ""


class MemoryManager:
    """
    上下文记忆管理器
    
    维护对话历史和项目修改记录，支持：
    - 多轮对话记忆
    - 工具使用跟踪
    - 文件修改记录
    - 记忆摘要生成
    """
    
    def __init__(
        self,
        max_history: int = 50,
        max_file_records: int = 100,
        session_id: Optional[str] = None
    ):
        """
        初始化记忆管理器
        
        Args:
            max_history: 最大保存的交互轮数
            max_file_records: 最大保存的文件修改记录数
            session_id: 会话 ID
        """
        self.max_history = max_history
        self.max_file_records = max_file_records
        
        # 生成会话 ID
        if session_id is None:
            session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_id = session_id
        
        # 交互历史
        self.interactions: List[Interaction] = []
        
        # 文件修改历史
        self.file_modifications: List[FileModification] = []
        
        # 工具使用统计
        self.tool_usage: Dict[str, int] = {}
        
        # 当前交互的缓冲区
        self._current_tools: List[str] = []
        self._current_tool_results: List[str] = []
        self._current_modified_files: List[str] = []
    
    # =========================================================================
    # 交互记录管理
    # =========================================================================
    
    def start_interaction(self):
        """开始一个新的交互（准备记录用户输入）"""
        self._current_tools = []
        self._current_tool_results = []
        self._current_modified_files = []
    
    def record_tool_use(
        self,
        tool_name: str,
        result: str = "",
        modified_file: Optional[str] = None
    ):
        """
        记录工具使用
        
        Args:
            tool_name: 工具名称
            result: 工具执行结果
            modified_file: 修改的文件（如果有）
        """
        self._current_tools.append(tool_name)
        if result:
            # 截断过长的结果
            if len(result) > 1000:
                result = result[:1000] + "... [结果已截断]"
            self._current_tool_results.append(result)
        
        if modified_file:
            self._current_modified_files.append(modified_file)
        
        # 更新工具使用统计
        self.tool_usage[tool_name] = self.tool_usage.get(tool_name, 0) + 1
    
    def add_interaction(
        self,
        user_input: str,
        ai_response: str
    ) -> Interaction:
        """
        添加一轮对话交互
        
        Args:
            user_input: 用户输入
            ai_response: AI 回复
        
        Returns:
            创建的交互记录
        """
        interaction = Interaction(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_input=user_input,
            ai_response=ai_response,
            tools_used=self._current_tools.copy(),
            tool_results=self._current_tool_results.copy(),
            modified_files=self._current_modified_files.copy(),
            session_id=self.session_id
        )
        
        self.interactions.append(interaction)
        
        # 记录文件修改
        for file_path in self._current_modified_files:
            self.file_modifications.append(FileModification(
                timestamp=interaction.timestamp,
                file_path=file_path,
                action="modified",
                details="在交互中被修改"
            ))
        
        # 保持历史长度限制
        if len(self.interactions) > self.max_history:
            self.interactions = self.interactions[-self.max_history:]
        
        # 清空当前交互缓冲区
        self.start_interaction()
        
        return interaction
    
    def add_file_modification(
        self,
        file_path: str,
        action: str,
        details: str = ""
    ):
        """
        记录文件修改
        
        Args:
            file_path: 文件路径
            action: 操作类型 (created/modified/deleted)
            details: 详细信息
        """
        modification = FileModification(
            timestamp=datetime.now(timezone.utc).isoformat(),
            file_path=file_path,
            action=action,
            details=details
        )
        
        self.file_modifications.append(modification)
        
        # 保持记录长度限制
        if len(self.file_modifications) > self.max_file_records:
            self.file_modifications = self.file_modifications[-self.max_file_records:]
    
    # =========================================================================
    # 查询方法
    # =========================================================================
    
    def get_recent_context(self, n: int = 10) -> str:
        """
        获取最近 N 轮交互的摘要
        
        Args:
            n: 交互轮数
        
        Returns:
            格式化的上下文摘要
        """
        if not self.interactions:
            return "暂无对话历史"
        
        recent = self.interactions[-n:] if len(self.interactions) >= n else self.interactions
        lines = []
        
        lines.append(f"## 最近 {len(recent)} 轮对话\n")
        
        for i, interaction in enumerate(recent, 1):
            lines.append(f"### 第 {i} 轮")
            lines.append(f"**时间**: {interaction.timestamp[:19]}")
            lines.append(f"**用户**: {interaction.user_input[:100]}{'...' if len(interaction.user_input) > 100 else ''}")
            lines.append(f"**助手**: {interaction.ai_response[:100]}{'...' if len(interaction.ai_response) > 100 else ''}")
            
            if interaction.tools_used:
                lines.append(f"**工具**: {', '.join(interaction.tools_used)}")
            
            if interaction.modified_files:
                lines.append(f"**修改文件**: {', '.join(interaction.modified_files)}")
            
            lines.append("")
        
        return "\n".join(lines)
    
    def get_interaction_history(
        self,
        include_tools: bool = True,
        include_results: bool = False
    ) -> List[Dict]:
        """
        获取交互历史（用于 Agent 上下文）
        
        Args:
            include_tools: 是否包含工具使用信息
            include_results: 是否包含工具执行结果
        
        Returns:
            交互历史列表
        """
        history = []
        
        for interaction in self.interactions:
            entry = {
                "timestamp": interaction.timestamp,
                "user": interaction.user_input,
                "assistant": interaction.ai_response,
            }
            
            if include_tools and interaction.tools_used:
                entry["tools"] = interaction.tools_used
            
            if include_results and interaction.tool_results:
                entry["results"] = interaction.tool_results
            
            if interaction.modified_files:
                entry["modified_files"] = interaction.modified_files
            
            history.append(entry)
        
        return history
    
    def get_modified_files(self) -> List[str]:
        """
        获取所有修改过的文件列表
        
        Returns:
            文件路径列表（去重）
        """
        files = set()
        
        # 从交互历史中获取
        for interaction in self.interactions:
            files.update(interaction.modified_files)
        
        # 从文件修改记录中获取
        for modification in self.file_modifications:
            files.add(modification.file_path)
        
        return sorted(list(files))
    
    def get_tool_usage_stats(self) -> Dict[str, int]:
        """
        获取工具使用统计
        
        Returns:
            工具名称 -> 使用次数 的字典
        """
        return self.tool_usage.copy()
    
    def search_interactions(
        self,
        keyword: str,
        case_sensitive: bool = False
    ) -> List[Interaction]:
        """
        搜索包含关键词的交互
        
        Args:
            keyword: 搜索关键词
            case_sensitive: 是否区分大小写
        
        Returns:
            匹配的交互列表
        """
        results = []
        
        if not case_sensitive:
            keyword = keyword.lower()
        
        for interaction in self.interactions:
            # 搜索用户输入
            content = interaction.user_input if case_sensitive else interaction.user_input.lower()
            if keyword in content:
                results.append(interaction)
                continue
            
            # 搜索 AI 回复
            content = interaction.ai_response if case_sensitive else interaction.ai_response.lower()
            if keyword in content:
                results.append(interaction)
                continue
            
            # 搜索工具名
            for tool in interaction.tools_used:
                content = tool if case_sensitive else tool.lower()
                if keyword in content:
                    results.append(interaction)
                    break
        
        return results
    
    # =========================================================================
    # 摘要和导出
    # =========================================================================
    
    def summarize(self) -> str:
        """
        生成记忆摘要
        
        Returns:
            格式化的记忆摘要文本
        """
        if not self.interactions:
            return "空记忆 - 暂无对话历史"
        
        lines = []
        lines.append("## 记忆摘要")
        lines.append("")
        
        # 基本统计
        lines.append(f"**会话 ID**: {self.session_id}")
        lines.append(f"**总交互数**: {len(self.interactions)}")
        lines.append(f"**工具使用次数**: {sum(self.tool_usage.values())}")
        lines.append(f"**修改文件数**: {len(self.get_modified_files())}")
        lines.append("")
        
        # 常用工具
        if self.tool_usage:
            lines.append("**常用工具 TOP5**:")
            sorted_tools = sorted(
                self.tool_usage.items(),
                key=lambda x: x[1],
                reverse=True
            )[:5]
            for tool, count in sorted_tools:
                lines.append(f"  - {tool}: {count} 次")
            lines.append("")
        
        # 修改的文件
        modified_files = self.get_modified_files()
        if modified_files:
            lines.append(f"**修改的文件** ({len(modified_files)} 个):")
            for file_path in modified_files[:10]:
                lines.append(f"  - {file_path}")
            if len(modified_files) > 10:
                lines.append(f"  - ... 还有 {len(modified_files) - 10} 个文件")
            lines.append("")
        
        # 最近活动
        if self.interactions:
            last = self.interactions[-1]
            lines.append("**最近活动**:")
            lines.append(f"  时间: {last.timestamp[:19]}")
            lines.append(f"  用户: {last.user_input[:50]}{'...' if len(last.user_input) > 50 else ''}")
            lines.append(f"  助手: {last.ai_response[:50]}{'...' if len(last.ai_response) > 50 else ''}")
        
        return "\n".join(lines)
    
    def export_to_json(self) -> str:
        """
        导出记忆为 JSON 格式
        
        Returns:
            JSON 字符串
        """
        data = {
            "session_id": self.session_id,
            "max_history": self.max_history,
            "tool_usage": self.tool_usage,
            "interactions": [
                {
                    "timestamp": i.timestamp,
                    "user_input": i.user_input,
                    "ai_response": i.ai_response,
                    "tools_used": i.tools_used,
                    "modified_files": i.modified_files,
                }
                for i in self.interactions
            ],
            "file_modifications": [
                {
                    "timestamp": m.timestamp,
                    "file_path": m.file_path,
                    "action": m.action,
                    "details": m.details,
                }
                for m in self.file_modifications
            ]
        }
        
        return json.dumps(data, indent=2, ensure_ascii=False)
    
    def load_from_json(self, json_str: str) -> bool:
        """
        从 JSON 加载记忆
        
        Args:
            json_str: JSON 字符串
        
        Returns:
            是否加载成功
        """
        try:
            data = json.loads(json_str)
            
            self.session_id = data.get("session_id", self.session_id)
            self.tool_usage = data.get("tool_usage", {})
            
            self.interactions = [
                Interaction(
                    timestamp=i["timestamp"],
                    user_input=i["user_input"],
                    ai_response=i["ai_response"],
                    tools_used=i.get("tools_used", []),
                    modified_files=i.get("modified_files", []),
                )
                for i in data.get("interactions", [])
            ]
            
            self.file_modifications = [
                FileModification(
                    timestamp=m["timestamp"],
                    file_path=m["file_path"],
                    action=m["action"],
                    details=m.get("details", ""),
                )
                for m in data.get("file_modifications", [])
            ]
            
            return True
            
        except Exception as e:
            print(tr("core.memory_load_failed", error=str(e)))
            return False
    
    # =========================================================================
    # 管理方法
    # =========================================================================
    
    def clear(self):
        """清空所有记忆"""
        self.interactions = []
        self.file_modifications = []
        self.tool_usage = {}
        self.start_interaction()
    
    def clear_old_interactions(self, before_timestamp: str) -> int:
        """
        删除指定时间之前的交互
        
        Args:
            before_timestamp: 时间戳（ISO 格式）
        
        Returns:
            删除的交互数量
        """
        original_count = len(self.interactions)
        self.interactions = [
            i for i in self.interactions
            if i.timestamp >= before_timestamp
        ]
        return original_count - len(self.interactions)
    
    def get_stats(self) -> Dict:
        """
        获取记忆统计信息
        
        Returns:
            统计信息字典
        """
        return {
            "session_id": self.session_id,
            "total_interactions": len(self.interactions),
            "total_file_modifications": len(self.file_modifications),
            "total_tool_uses": sum(self.tool_usage.values()),
            "unique_tools_used": len(self.tool_usage),
            "unique_files_modified": len(self.get_modified_files()),
        }
    
    def __len__(self) -> int:
        """返回交互数量"""
        return len(self.interactions)
    
    def __repr__(self) -> str:
        return (
            f"MemoryManager("
            f"session={self.session_id}, "
            f"interactions={len(self.interactions)}, "
            f"files_modified={len(self.get_modified_files())}"
            f")"
        )