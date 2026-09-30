"""Vendor profiles registry and exports."""

from __future__ import annotations

from typing import Dict, Type

from hrms_plugin.connectors.profiles.bamboohr import BambooHRProfile
from hrms_plugin.connectors.profiles.base_profile import VendorProfile
from hrms_plugin.connectors.profiles.dynamic import DynamicVendorProfile
from hrms_plugin.connectors.profiles.frappe import FrappeProfile
from hrms_plugin.connectors.profiles.generic import GenericProfile
from hrms_plugin.connectors.profiles.iceipts import IceiptsProfile, extract_jwt_user_id
from hrms_plugin.connectors.profiles.workday import WorkdayProfile
from hrms_plugin.connectors.profiles.zoho import ZohoProfile

_PROFILES: Dict[str, Type[VendorProfile]] = {
    "generic": GenericProfile,
    "dynamic": DynamicVendorProfile,
    "iceipts": IceiptsProfile,
    "iceipts_hrms": IceiptsProfile,
    "frappe": FrappeProfile,
    "erpnext": FrappeProfile,
    "bamboohr": BambooHRProfile,
    "workday": WorkdayProfile,
    "zoho": ZohoProfile,
    "zoho_people": ZohoProfile,
}


def get_vendor_profile(vendor_name: str, **kwargs) -> VendorProfile:
    """Instantiate a vendor profile by name, defaulting to GenericProfile if unknown."""
    norm_name = (vendor_name or "generic").strip().lower()
    profile_cls = _PROFILES.get(norm_name, GenericProfile)
    return profile_cls(**kwargs)


__all__ = [
    "VendorProfile",
    "GenericProfile",
    "DynamicVendorProfile",
    "IceiptsProfile",
    "FrappeProfile",
    "BambooHRProfile",
    "WorkdayProfile",
    "ZohoProfile",
    "extract_jwt_user_id",
    "get_vendor_profile",
]
