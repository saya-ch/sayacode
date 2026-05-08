"""
API 配置向导

提供交互式 API 配置引导。
支持多 API 接口规范的分步骤配置流程。
"""

from typing import Optional
from urllib.parse import urlparse
import getpass
import os

from .api_config import APIConfig, APIConfigManager, APIType, _is_local_http_url
from ..i18n import tr
from ..models import parse_context_window
from ..models.provider_catalog import USER_VISIBLE_PROVIDER_TYPES, provider_defaults
from ..models.registry import get_model_provider_registry


class WizardConsole:
    """向导控制台接口"""

    def __init__(self, console=None):
        self.console = console

    def print(self, text: str = "", style: str = ""):
        """打印文本"""
        if self.console:
            if style:
                self.console.print(f"[{style}]{text}[/]")
            else:
                self.console.print(text)
        else:
            print(text)

    def input(self, prompt: str) -> str:
        """获取用户输入"""
        if self.console:
            return self.console.input(prompt)
        else:
            return input(prompt)

    def secret_input(self, prompt: str) -> str:
        """获取敏感输入，隐藏终端回显。"""
        if self.console:
            try:
                return self.console.input(prompt, password=True)
            except TypeError:
                return getpass.getpass(prompt)
        return getpass.getpass(prompt)

    def print_header(self, title: str):
        """打印标题"""
        separator = "=" * 50
        self.print(separator)
        self.print(title, "bold")
        self.print(separator)

    def print_step(self, step: int, total: int, title: str):
        """打印步骤"""
        self.print(tr("wizard.step_line", step=step, total=total, title=title))

    def print_error(self, msg: str):
        """打印错误"""
        self.print(tr("wizard.status_line", status=tr("common.error"), message=msg), "red")

    def print_success(self, msg: str):
        """打印成功"""
        self.print(tr("wizard.status_line", status=tr("common.success"), message=msg), "green")

    def print_warning(self, msg: str):
        """打印警告"""
        self.print(tr("wizard.status_line", status=tr("common.warning"), message=msg), "yellow")

    def print_info(self, msg: str):
        """打印信息"""
        self.print(tr("wizard.status_line", status=tr("common.info"), message=msg), "cyan")


class APIConfigWizard:
    """
    API 配置向导

    提供交互式配置引导，帮助用户配置不同类型的 API。
    """

    def __init__(self, console=None, manager: Optional[APIConfigManager] = None):
        """
        初始化配置向导

        Args:
            console: 控制台对象（Rich console）
        """
        self.console = WizardConsole(console)
        self.manager = manager or APIConfigManager()

    def run(self, config_name: Optional[str] = None) -> Optional[APIConfig]:
        """
        运行配置向导

        Args:
            config_name: 配置名称（用于保存/更新配置）

        Returns:
            创建的配置，如果取消返回 None
        """
        self.console.print_header(tr("wizard.title"))

        # 步骤 1: 选择接口规范
        api_type = self._select_api_type()
        if api_type is None:
            self.console.print_warning(tr("wizard.cancelled"))
            return None

        # 步骤 2: 输入 Base URL
        base_url = self._input_base_url(api_type)
        if base_url is None:
            self.console.print_warning(tr("wizard.cancelled"))
            return None

        # 步骤 3: 输入 API Key
        api_key = self._input_api_key(api_type, base_url)
        if api_key is None:
            self.console.print_warning(tr("wizard.cancelled"))
            return None

        # 步骤 4: 输入模型名称
        model_name = self._input_model_name(api_type)
        if model_name is None:
            self.console.print_warning(tr("wizard.cancelled"))
            return None

        # 步骤 5: 其他配置（可选）
        extra_config = self._input_extra_config(api_type)
        context_window = self._resolve_context_window(
            api_type,
            base_url,
            api_key,
            model_name,
            extra_config,
        )
        extra_config["context_window"] = context_window

        # 创建配置
        config = APIConfig(
            api_type=api_type,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            **extra_config
        )

        # 验证配置
        is_valid, error_msg = config.validate()
        if not is_valid:
            self.console.print_error(tr("wizard.validation_failed", error=error_msg))
            return None

        # 保存配置
        if config_name is None:
            config_name = self._get_default_config_name(api_type)

        if self.manager.add_config(config_name, config):
            self.manager.set_current(config_name)
            self.console.print_success(tr("wizard.profile_saved", name=config_name))
        else:
            self.console.print_error(tr("wizard.profile_save_failed"))
            return None

        return config

    def _resolve_context_window(
        self,
        api_type: APIType,
        base_url: str,
        api_key: str,
        model_name: str,
        extra_config: dict,
    ) -> int:
        """探测上下文窗口；无法准确探测时要求用户输入。"""
        self.console.print()
        self.console.print_info(tr("configure.context_window_detecting"))

        try:
            detected = get_model_provider_registry().detect_context_window(
                api_type=api_type.value,
                model_name=model_name,
                base_url=base_url,
                api_key=api_key,
                **extra_config,
            )
            if detected:
                self.console.print_success(tr("connection.context_detected", context_window=f"{detected:,}"))
                return detected
        except Exception:
            pass

        self.console.print_warning(tr("wizard.context_window_unknown"))
        self.console.print_warning(tr("configure.context_window_accuracy_hint"))
        self.console.print_info(tr("configure.context_window_format_hint"))

        while True:
            raw_value = self.console.input(tr("wizard.context_window_input")).strip()
            parsed = parse_context_window(raw_value)
            if parsed:
                self.console.print_success(tr("configure.context_window_set", context_window=f"{parsed:,}"))
                return parsed
            self.console.print_error(tr("configure.context_window_invalid"))

    def _select_api_type(self) -> Optional[APIType]:
        """
        选择接口规范

        Returns:
            APIType 或 None（取消）
        """
        self.console.print_step(1, 5, tr("wizard.step_protocol"))
        self.console.print()

        # 显示选项
        api_types = _visible_api_types()
        for i, api_type in enumerate(api_types, 1):
            self.console.print(f"  {i}. {_api_type_display_name(api_type)}")

        self.console.print()
        self.console.print_info(tr("wizard.select_hint"))

        while True:
            choice = self.console.input(tr("wizard.select_prompt", count=len(api_types))).strip()

            if choice.lower() == 'q':
                return None

            try:
                index = int(choice) - 1
                if 0 <= index < len(api_types):
                    selected = api_types[index]
                    self.console.print_success(tr("wizard.protocol_selected", name=_api_type_display_name(selected)))
                    return selected
                else:
                    self.console.print_error(tr("wizard.invalid_choice", count=len(api_types)))
            except ValueError:
                self.console.print_error(tr("wizard.invalid_number"))

    def _input_base_url(self, api_type: APIType) -> Optional[str]:
        """
        输入 Base URL

        Args:
            api_type: API 类型

        Returns:
            Base URL 或 None（取消）
        """
        self.console.print_step(2, 5, tr("wizard.step_base_url"))
        self.console.print()

        default_url = api_type.default_base_url
        self.console.print(tr("wizard.default_url", url=default_url))
        self.console.print()
        self.console.print_info(tr("wizard.examples"))
        for option in _visible_api_types():
            defaults = provider_defaults(option.value)
            self.console.print(f"  - {_api_type_display_name(option)}: {defaults['default_base_url']}")
        self.console.print()

        while True:
            url = self.console.input(tr("wizard.base_url_prompt")).strip()

            # 使用默认值
            if not url:
                url = default_url

            if url.lower() == 'q':
                return None

            # 验证 URL
            is_valid, error_msg = self._validate_base_url(url, api_type)
            if is_valid:
                self.console.print_success(tr("wizard.url_selected", url=url))
                return url
            else:
                self.console.print_error(error_msg)
                self.console.print_info(tr("wizard.cancel_hint"))

    def _input_api_key(self, api_type: APIType, base_url: Optional[str] = None) -> Optional[str]:
        """
        输入 API Key

        Args:
            api_type: API 类型

        Returns:
            API Key 或 None（取消/跳过）
        """
        self.console.print_step(3, 5, tr("wizard.step_api_key"))
        self.console.print()

        if not api_type.requires_api_key:
            self.console.print_info(tr("wizard.api_key_not_required"))
            return ""

        env_name = api_type.api_key_env
        has_env_key = bool(env_name and os.environ.get(env_name))
        default_base_url = api_type.default_base_url.rstrip("/")
        actual_base_url = str(base_url or "").rstrip("/")
        requires_key_now = actual_base_url == default_base_url and not has_env_key

        self.console.print_warning(tr("wizard.api_key_storage_warning"))
        self.console.print_info(tr("wizard.api_key_skip_hint"))

        while True:
            api_key = self.console.input(tr("wizard.api_key_prompt")).strip()

            if api_key.lower() == 'q':
                return None

            if api_key.lower() == 's' or (not api_key and not requires_key_now):
                self.console.print_info(tr("wizard.api_key_skipped"))
                return ""

            # 验证 API Key
            is_valid, error_msg = self._validate_api_key(api_key, api_type)
            if is_valid:
                # 确认输入
                confirm = self.console.input(tr("wizard.confirm_secret")).strip().lower()
                if confirm == 'y':
                    return api_key
            else:
                self.console.print_error(error_msg)
                self.console.print_info(tr("wizard.api_key_retry_hint"))

    def _input_model_name(self, api_type: APIType) -> Optional[str]:
        """
        输入模型名称

        Args:
            api_type: API 类型

        Returns:
            模型名称或 None（取消）
        """
        self.console.print_step(4, 5, tr("wizard.step_model_name"))
        self.console.print()

        default_model = api_type.default_model
        self.console.print(tr("wizard.default_model", model=default_model))
        self.console.print()
        self.console.print_info(tr("wizard.examples"))
        for option in _visible_api_types():
            defaults = provider_defaults(option.value)
            self.console.print(f"  - {_api_type_display_name(option)}: {defaults['default_model_name']}")
        self.console.print()

        while True:
            model_name = self.console.input(tr("wizard.model_name_prompt")).strip()

            if model_name.lower() == 'q':
                return None

            # 使用默认值
            if not model_name:
                model_name = default_model

            if not model_name.strip():
                self.console.print_error(tr("wizard.model_name_required"))
                continue

            self.console.print_success(tr("wizard.model_selected", model=model_name))
            return model_name

    def _input_extra_config(self, api_type: APIType) -> dict:
        """
        输入额外配置

        Args:
            api_type: API 类型

        Returns:
            额外配置字典
        """
        config = {}

        self.console.print_step(5, 5, tr("wizard.step_extra"))
        self.console.print(tr("wizard.extra_optional"))
        self.console.print()

        # 温度参数
        temp = self.console.input(tr("wizard.temperature_prompt")).strip()
        if temp:
            try:
                temp_val = float(temp)
                config['temperature'] = max(0.0, min(1.0, temp_val))
            except ValueError:
                self.console.print_warning(tr("wizard.invalid_temperature"))

        # 超时时间
        timeout = self.console.input(tr("wizard.timeout_prompt")).strip()
        if timeout:
            try:
                config['timeout'] = int(timeout)
            except ValueError:
                self.console.print_warning(tr("wizard.invalid_timeout"))

        # 最大重试次数
        retries = self.console.input(tr("wizard.retries_prompt")).strip()
        if retries:
            try:
                config['max_retries'] = int(retries)
            except ValueError:
                self.console.print_warning(tr("wizard.invalid_retries"))

        # Azure 特定配置
        if api_type == APIType.AZURE_OPENAI:
            self.console.print_info(tr("wizard.azure_config"))

            api_version = self.console.input(tr("wizard.azure_api_version_prompt")).strip()
            if api_version:
                config['azure_api_version'] = api_version

            deployment = self.console.input(tr("wizard.azure_deployment_prompt")).strip()
            if deployment:
                config['azure_deployment'] = deployment

        return config

    def _get_default_config_name(self, api_type: APIType) -> str:
        """
        获取默认配置名称

        Args:
            api_type: API 类型

        Returns:
            配置名称
        """
        name_base = api_type.value
        existing = self.manager.list_configs()

        # 检查是否已存在
        if name_base not in existing:
            return name_base

        # 生成新名称
        counter = 1
        while True:
            new_name = f"{name_base}_{counter}"
            if new_name not in existing:
                return new_name
            counter += 1

    def _validate_base_url(self, url: str, api_type: APIType) -> tuple[bool, str]:
        """
        验证 Base URL

        Args:
            url: URL 字符串
            api_type: API 类型

        Returns:
            (是否有效, 错误消息)
        """
        if not url:
            return False, tr("wizard.url_required")

        # 检查基本格式
        if not url.startswith(('http://', 'https://')):
            return False, tr("wizard.url_scheme_required")

        # 解析 URL 检查格式
        try:
            parsed = urlparse(url)
            if not parsed.netloc:
                return False, tr("wizard.url_host_required")
        except Exception as e:
            return False, tr("wizard.url_invalid", error=str(e))

        # Ollama 允许 HTTP，本地或远程均可用于自托管服务。
        if api_type == APIType.OLLAMA:
            return True, ""

        # 其他类型仅允许本机 HTTP，非本机端点必须使用 HTTPS。
        if parsed.scheme == "http" and not _is_local_http_url(url):
            return False, tr("wizard.url_http_warning")

        return True, ""

    def _validate_api_key(self, key: str, api_type: APIType) -> tuple[bool, str]:
        """
        验证 API Key

        Args:
            key: API Key
            api_type: API 类型

        Returns:
            (是否有效, 错误消息)
        """
        if not key:
            return False, tr("wizard.api_key_required")

        return True, ""

    def test_connection(self, config: APIConfig) -> bool:
        """
        测试 API 连接

        Args:
            config: API 配置

        Returns:
            连接是否成功
        """
        self.console.print(tr("wizard.connection_testing"))
        self.console.print_info(f"{tr('wizard.field_url')}: {config.base_url}")
        self.console.print_info(tr("wizard.model_info", model=config.model_name))

        try:
            model = get_model_provider_registry().create_from_config(config)

            # 简单测试
            test_messages = [{"role": "user", "content": "Hi"}]
            model.chat(test_messages)

            self.console.print_success(tr("connection.connected"))
            return True

        except Exception as e:
            self.console.print_error(tr("connection.failed", error=str(e)))
            return False


class APIConfigWizardCLI:
    """
    API 配置向导 CLI 入口

    提供命令行界面的配置向导。
    """

    def __init__(self, console=None, manager: Optional[APIConfigManager] = None):
        self.wizard = APIConfigWizard(console, manager=manager)

    def run(self, args: list = None) -> int:
        """
        运行 CLI

        Args:
            args: 命令行参数

        Returns:
            退出码 (0 成功, 1 失败)
        """
        if not args:
            args = []

        if not args:
            # 无参数，运行完整向导
            return self._run_wizard()

        # 解析子命令
        subcommand = args[0].lower() if args else ""

        if subcommand == "add":
            # 添加新配置
            config_name = args[1] if len(args) > 1 else None
            return self._run_wizard(config_name)

        elif subcommand == "list":
            # 列出所有配置
            return self._list_configs()

        elif subcommand == "show":
            # 显示配置详情
            config_name = args[1] if len(args) > 1 else None
            return self._show_config(config_name)

        elif subcommand == "set":
            # 设置当前配置
            if len(args) < 2:
                self.wizard.console.print_error(tr("wizard.usage_set"))
                return 1
            return self._set_current(args[1])

        elif subcommand == "delete":
            # 删除配置
            if len(args) < 2:
                self.wizard.console.print_error(tr("wizard.usage_delete"))
                return 1
            return self._delete_config(args[1])

        elif subcommand == "test":
            # 测试配置
            if len(args) < 2:
                self.wizard.console.print_error(tr("wizard.usage_test"))
                return 1
            return self._test_config(args[1])

        elif subcommand in ["help", "-h", "--help"]:
            # 显示帮助
            return self._show_help()

        else:
            self.wizard.console.print_error(tr("wizard.unknown_command", command=subcommand))
            return self._show_help()

    def _run_wizard(self, config_name: str = None) -> int:
        """运行配置向导"""
        config = self.wizard.run(config_name)
        if config:
            return 0
        return 1

    def _list_configs(self) -> int:
        """列出所有配置"""
        configs = self.wizard.manager.list_configs()

        if not configs:
            self.wizard.console.print_info(tr("wizard.no_profiles"))
            self.wizard.console.print_info(tr("wizard.add_profile_hint"))
            return 0

        self.wizard.console.print_header(tr("wizard.saved_profiles"))

        for name in configs:
            details = self.wizard.manager.get_config_details(name)
            if details:
                current_mark = tr("wizard.current_marker") if details['is_current'] else ""
                api_type = APIType.from_value(details.get("api_type", ""))
                api_label = _api_type_display_name(api_type) if api_type else details.get("api_type", "")
                self.wizard.console.print(f"\n  {name}{current_mark}")
                self.wizard.console.print(f"    {tr('wizard.field_type')}: {api_label}")
                self.wizard.console.print(f"    {tr('wizard.field_model')}: {details['model_name']}")
                self.wizard.console.print(f"    {tr('wizard.field_url')}: {details['base_url']}")

        return 0

    def _show_config(self, name: str = None) -> int:
        """显示配置详情"""
        if not name:
            # 显示当前配置
            config = self.wizard.manager.get_current_config()
            if not config:
                self.wizard.console.print_info(tr("wizard.no_current_profile"))
                return 0
            name = self.wizard.manager.current_config_name

        details = self.wizard.manager.get_config_details(name)
        if not details:
            self.wizard.console.print_error(tr("wizard.profile_not_found", name=name))
            return 1

        self.wizard.console.print_header(tr("wizard.profile_details", name=name))

        for key, value in details.items():
            self.wizard.console.print(f"  {key}: {value}")

        return 0

    def _set_current(self, name: str) -> int:
        """设置当前配置"""
        if self.wizard.manager.set_current(name):
            self.wizard.console.print_success(tr("wizard.current_set", name=name))
            return 0
        else:
            self.wizard.console.print_error(tr("wizard.profile_not_found", name=name))
            return 1

    def _delete_config(self, name: str) -> int:
        """删除配置"""
        if self.wizard.manager.delete_config(name):
            self.wizard.console.print_success(tr("wizard.profile_deleted", name=name))
            return 0
        else:
            self.wizard.console.print_error(tr("wizard.profile_not_found", name=name))
            return 1

    def _test_config(self, name: str) -> int:
        """测试配置"""
        config = self.wizard.manager.get_config(name)
        if not config:
            self.wizard.console.print_error(tr("wizard.profile_not_found", name=name))
            return 1

        if self.wizard.test_connection(config):
            return 0
        return 1

    def _show_help(self) -> int:
        """显示帮助"""
        self.wizard.console.print(tr("wizard.help"))
        return 0


def _api_type_display_name(api_type: APIType) -> str:
    return tr(f"api_type.{api_type.value}")


def _visible_api_types() -> list[APIType]:
    return [
        api_type
        for value in USER_VISIBLE_PROVIDER_TYPES
        if (api_type := APIType.from_value(value)) is not None
    ]
