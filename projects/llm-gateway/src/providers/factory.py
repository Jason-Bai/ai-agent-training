from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from providers.base import Provider
from providers.deepseek import DeepSeekProvider
from providers.mock import MockProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.bailian import BailianProvider
from services.credentials import load_credentials
from services.models import ConfigurationError, DEFAULT_CONFIG_DIR, ModelRegistry, ProviderConfig


def create_provider(config: ProviderConfig, credential: str | None = None) -> Provider:
    if config.adapter == 'mock':
        return MockProvider()
    if not credential or not credential.strip():
        raise ValueError('真实 Provider 缺少有效凭据')
    implementation = {'deepseek':DeepSeekProvider, 'openai_compatible':OpenAICompatibleProvider,
                      'bailian':BailianProvider}[config.adapter]
    return implementation(config.base_url, credential)


class ProviderClients:
    """Prepare Provider dependencies; own only clients created during open()."""

    def __init__(self, registry: ModelRegistry, *, provider: Provider | None = None,
                 providers: Mapping[str, Provider] | None = None,
                 config_dir: Path = DEFAULT_CONFIG_DIR):
        if provider is not None and providers is not None:
            raise ValueError("Provider 只能选择一种显式注入方式")
        injected = ({name: provider for name in registry.providers} if provider is not None
                    else dict(providers) if providers is not None else None)
        if injected is not None and set(injected) != set(registry.providers):
            raise ValueError("注入的 Provider 集合与注册表不匹配")
        implementations = {'mock': MockProvider, 'deepseek': DeepSeekProvider,
                           'openai_compatible': OpenAICompatibleProvider, 'bailian':BailianProvider}
        for model in registry.endpoints.values():
            bound = (injected[model.provider] if injected is not None else
                     implementations[registry.providers[model.provider].adapter])
            if model.supports_streaming and not callable(getattr(bound, 'stream', None)):
                raise ConfigurationError('支持流式的模型要求 Provider 实现 stream')
        self.instances = MappingProxyType(injected if injected is not None else {})
        self._injected = injected is not None
        self._definitions = registry.providers
        refs = {definition.credential_ref for definition in self._definitions.values()
                if definition.adapter != 'mock'}
        # Explicit Provider objects are complete dependencies, not credential hints.
        self._credentials = load_credentials(config_dir / 'credentials.yaml', refs) if not self._injected else {}

    @asynccontextmanager
    async def open(self):
        owned = []
        try:
            if not self._injected:
                constructed = {}
                for name, definition in self._definitions.items():
                    secret = self._credentials.get(definition.credential_ref)
                    instance = create_provider(definition, secret.get_secret_value() if secret else None)
                    owned.append(instance)
                    constructed[name] = instance
                self.instances = MappingProxyType(constructed)
            yield self.instances
        finally:
            failure = False
            for instance in reversed(owned):
                try:
                    await instance.aclose()
                except Exception:
                    failure = True
            if failure:
                raise RuntimeError("Provider 客户端关闭失败") from None
