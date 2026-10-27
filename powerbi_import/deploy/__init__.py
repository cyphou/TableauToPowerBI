"""
Fabric deployment subpackage.

Provides authentication, HTTP client, and deployment orchestration
for publishing Power BI projects to Microsoft Fabric workspaces
and Power BI Service.
"""

from .auth import FabricAuthenticator
from .bundle_deployer import (
    BundleDeployer,
    BundleDeploymentResult,
    deploy_bundle_from_cli,
)
from .client import FabricClient
from .deployer import FabricDeployer
from .pbi_client import PBIServiceClient
from .pbi_deployer import DeploymentResult, PBIWorkspaceDeployer
from .pbix_packager import PBIXPackager
from .utils import ArtifactCache, DeploymentReport

__all__ = [
    'FabricAuthenticator',
    'FabricClient',
    'FabricDeployer',
    'DeploymentReport',
    'ArtifactCache',
    'PBIServiceClient',
    'PBIXPackager',
    'PBIWorkspaceDeployer',
    'DeploymentResult',
    'BundleDeployer',
    'BundleDeploymentResult',
    'deploy_bundle_from_cli',
]
