"""
Django admin registrations.

This is the Phase 4 administration surface in its cheapest form: enough to add
sites, areas, streams and devices without writing a management screen for each.
"""
from django.contrib import admin
from .models import (
    AuditEntry, Calibration, CalibrationSample, DeclaredPurity, Device, Flag,
    PurityCheck, SackLog, Site, SourceArea, Stream, Staff,
)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "type")


@admin.register(Stream)
class StreamAdmin(admin.ModelAdmin):
    list_display = ("name", "recoverable", "colour", "sort_order")
    list_editable = ("recoverable", "sort_order")


@admin.register(SourceArea)
class SourceAreaAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "denominator_type", "denominator_value",
                    "public_facing", "archived")
    list_filter = ("site", "public_facing", "archived")


class CalibrationSampleInline(admin.TabularInline):
    model = CalibrationSample
    extra = 0


@admin.register(Calibration)
class CalibrationAdmin(admin.ModelAdmin):
    list_display = ("stream", "site", "mean_kg", "sd_kg", "n", "measured_on")
    list_filter = ("site", "stream")
    inlines = [CalibrationSampleInline]


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("label", "site", "role", "pair_code", "last_seen")
    # The token is the device's credential; it is generated, never typed.
    readonly_fields = ("token",)


@admin.register(SackLog)
class SackLogAdmin(admin.ModelAdmin):
    list_display = ("logged_at", "source_area", "stream", "count", "fill",
                    "method", "location_verified", "voided")
    list_filter = ("stream", "source_area", "method", "voided")
    date_hierarchy = "logged_at"


@admin.register(PurityCheck)
class PurityCheckAdmin(admin.ModelAdmin):
    list_display = ("checked_at", "source_area", "stream", "items_checked",
                    "items_wrong", "purity_display", "main_contaminant")
    list_filter = ("stream", "source_area")

    @admin.display(description="Purity")
    def purity_display(self, obj):
        return f"{obj.purity:.0%}" if obj.purity is not None else "-"


@admin.register(DeclaredPurity)
class DeclaredPurityAdmin(admin.ModelAdmin):
    list_display = ("source_area", "stream", "value", "declared_by", "declared_on")
    # Model.clean rejects a declaration on a public-facing area; the admin form
    # calls clean, so the refusal surfaces as a normal validation message.


@admin.register(Flag)
class FlagAdmin(admin.ModelAdmin):
    list_display = ("sacklog", "kind", "resolved", "resolved_by")
    list_filter = ("kind", "resolved")


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ("at", "actor", "action", "target")
    readonly_fields = ("at",)


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    """
    Supervisors and admins. Janitors are NOT here - they are under Devices,
    because they have no account by design.
    """
    list_display = ("user", "role", "site", "created_by", "created_on")
    list_filter = ("role", "site")
    search_fields = ("user__username", "user__first_name")
