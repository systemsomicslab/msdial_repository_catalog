from .interactive_bridge import InteractiveAdapterBridge
from .mbpost import MbPostAdapter
from .metabobank import MetaboBankAdapter
from .metabolights import MetaboLightsAdapter
from .workbench import MetabolomicsWorkbenchAdapter

NATIVE_ADAPTERS = {
    "metabolomics_workbench": MetabolomicsWorkbenchAdapter,
    "metabolights": MetaboLightsAdapter,
    "mb_post": MbPostAdapter,
    "metabobank": MetaboBankAdapter,
}


def native_adapter(repository: str):
    try:
        return NATIVE_ADAPTERS[repository]()
    except KeyError as error:
        raise ValueError(f"Unknown repository: {repository}") from error


__all__ = [
    "InteractiveAdapterBridge",
    "MbPostAdapter",
    "MetaboBankAdapter",
    "MetaboLightsAdapter",
    "MetabolomicsWorkbenchAdapter",
    "NATIVE_ADAPTERS",
    "native_adapter",
]
