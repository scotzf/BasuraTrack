"""
Sukod data model.

Design notes that matter more than the field list:

* A SackLog records the SOURCE AREA the waste came from, which is deliberately
  independent of the DEVICE that logged it. That is what makes centralised
  logging possible: one person at the staging point logs sacks for six
  buildings from a single phone.

* The primary key of SackLog is a client-generated UUID (Universally Unique
  Identifier). The phone creates it before the entry ever reaches the network.
  The server ignores a UUID it already holds, so replaying a sync queue can
  never double-count.

* Nothing here links an entry to a named human. There is no "logged_by_person"
  field and there never will be one: logged volume must never become a
  performance measure for the janitor.
"""
import uuid
from django.db import models

# Fill level -> multiplier applied to the calibrated full-sack weight.
# A two-thirds sack weighs two-thirds of a full one.
FILL_FACTOR = {
    "full": 1.00,
    "two_thirds": 0.67,
    "half": 0.50,
}
FILL_CHOICES = [
    ("full", "Full"),
    ("two_thirds", "Two-thirds"),
    ("half", "Half"),
]

# Method tag - how the mass on this entry was arrived at. Every displayed
# figure carries the tags of the entries behind it.
METHOD_CHOICES = [
    ("C", "Counted and calibrated"),
    ("W", "Weighed"),
    ("V", "Volume-estimated"),
    ("I", "Imputed"),
]


def new_hex_token():
    """Random 32-character token. A named function, not a lambda, because
    Django has to write this default into a migration file as an import path."""
    return uuid.uuid4().hex


class Site(models.Model):
    """A campus, LGU (Local Government Unit) or establishment. Top of the tree."""
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=40, default="campus")
    # Junkshop buy-back prices, {stream_name: peso_per_kg}. Used for the
    # optional value estimate on the dashboard.
    junkshop_rates_json = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class Stream(models.Model):
    """Biodegradable, Recyclable, Residual, Mixed."""
    name = models.CharField(max_length=40, unique=True)
    # Recoverable streams are the ones that count toward diversion.
    recoverable = models.BooleanField(default=False)
    colour = models.CharField(max_length=7, default="#888888")
    # Display order on the logger screen, so the four counters never reshuffle
    # under the thumb of someone who has learned their positions.
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class SourceArea(models.Model):
    """
    A building or location whose waste is collected at one staging point.

    denominator_type / denominator_value exist so per-area comparison can be
    normalised. Raw totals always rank the largest building worst, which tells
    you nothing you did not already know.
    """
    DENOMINATORS = [
        ("persons", "Persons per day"),
        ("meals", "Meals served per day"),
        ("room_nights", "Room-nights per day"),
    ]
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="areas")
    name = models.CharField(max_length=120)
    denominator_type = models.CharField(max_length=20, choices=DENOMINATORS, default="persons")
    denominator_value = models.FloatField(default=1)
    qr_token = models.CharField(max_length=32, default=new_hex_token, unique=True)
    # public_facing bins are filled by anyone walking past. Their purity can
    # never be declared - it must be measured. Enforced in DeclaredPurity.clean.
    public_facing = models.BooleanField(default=True)
    archived = models.BooleanField(default=False)
    # Rough centre of the staging point, for the background location check.
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Calibration(models.Model):
    """
    A weighing session that establishes mean kg per full sack for one stream.

    n and sd_kg are what turn a sack count into a number with an honest error
    band. A calibration of n=1 is not a calibration.
    """
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="calibrations")
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE)
    mean_kg = models.FloatField()
    sd_kg = models.FloatField()
    n = models.PositiveIntegerField()
    measured_on = models.DateField()

    class Meta:
        # Newest first: the current calibration for a stream is the latest one.
        ordering = ["-measured_on", "-id"]

    @property
    def standard_error(self):
        """
        sd / sqrt(n) - how well we know the AVERAGE sack, as opposed to how
        much individual sacks vary. Every entry's error band is this number
        multiplied by the count and the fill factor.
        """
        import math
        if not self.n:
            return 0.0
        return self.sd_kg / math.sqrt(self.n)

    def __str__(self):
        return f"{self.stream} {self.mean_kg:.1f}+-{self.sd_kg:.1f} kg (n={self.n})"


class CalibrationSample(models.Model):
    """One sack put on the scale during a calibration session."""
    calibration = models.ForeignKey(Calibration, on_delete=models.CASCADE, related_name="samples")
    kg = models.FloatField()


class Device(models.Model):
    """
    A paired phone. Loggers never see a login screen; they type a pairing code
    once and the device holds a token from then on.
    """
    ROLES = [("logger", "Logger"), ("supervisor", "Supervisor"), ("admin", "Admin")]
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="devices")
    label = models.CharField(max_length=80)
    role = models.CharField(max_length=20, choices=ROLES, default="logger")
    pair_code = models.CharField(max_length=8, unique=True)
    token = models.CharField(max_length=64, default=new_hex_token)
    paired_at = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.label


class SackLog(models.Model):
    """One line in the notebook: N sacks of one stream from one area."""
    # Client-generated. This is the whole duplicate-protection story.
    id_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_area = models.ForeignKey(SourceArea, on_delete=models.PROTECT, related_name="logs")
    stream = models.ForeignKey(Stream, on_delete=models.PROTECT)
    count = models.PositiveIntegerField()
    fill = models.CharField(max_length=12, choices=FILL_CHOICES, default="full")
    method = models.CharField(max_length=1, choices=METHOD_CHOICES, default="C")
    # Only set when method == "W": a real scale reading, no estimation error.
    weighed_kg = models.FloatField(null=True, blank=True)
    logged_at = models.DateTimeField()
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    # False when no GPS (Global Positioning System) fix arrived in time, or the
    # fix was far from the area. Saving is never blocked on location.
    location_verified = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)
    voided = models.BooleanField(default=False)   # undo, keeps the audit trail

    class Meta:
        ordering = ["-logged_at"]


class PurityCheck(models.Model):
    """
    A spot-check: pull items out of one stream's sack and count how many do not
    belong. This is the measurement everyone else skips.
    """
    source_area = models.ForeignKey(SourceArea, on_delete=models.CASCADE, related_name="purity_checks")
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE)
    items_checked = models.PositiveIntegerField()
    items_wrong = models.PositiveIntegerField()
    main_contaminant = models.CharField(max_length=120, blank=True)
    checked_at = models.DateTimeField()
    by_device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-checked_at"]

    @property
    def purity(self):
        """purity = 1 - wrong/checked. Zero items checked is not a measurement."""
        if not self.items_checked:
            return None
        return 1 - (self.items_wrong / self.items_checked)


class DeclaredPurity(models.Model):
    """
    A stated purity for a stream whose bin only trained staff can fill -
    kitchen cardboard, office paper, bar bottles.

    This is an assumption, not a measurement, so it is rendered with a distinct
    marker and named in every report's limitations section.
    """
    source_area = models.ForeignKey(SourceArea, on_delete=models.CASCADE, related_name="declared_purities")
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE)
    value = models.FloatField()
    reason = models.CharField(max_length=200)
    declared_by = models.CharField(max_length=80)
    declared_on = models.DateField()

    class Meta:
        unique_together = [("source_area", "stream")]

    def clean(self):
        """A public-facing bin can be filled by anybody, so its purity is not
        something anyone is in a position to declare. Refuse it."""
        from django.core.exceptions import ValidationError
        if self.source_area.public_facing:
            raise ValidationError(
                "Purity cannot be declared for a public-facing area. Measure it."
            )


class Flag(models.Model):
    """Something a supervisor should look at: location mismatch, outlier count."""
    sacklog = models.ForeignKey(SackLog, on_delete=models.CASCADE, related_name="flags")
    kind = models.CharField(max_length=40)
    resolved = models.BooleanField(default=False)
    resolved_by = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=200, blank=True)


class AuditEntry(models.Model):
    """Every edit to a logged figure, with before and after."""
    actor = models.CharField(max_length=80)
    action = models.CharField(max_length=40)
    target = models.CharField(max_length=120)
    before_json = models.JSONField(null=True, blank=True)
    after_json = models.JSONField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True)


class Staff(models.Model):
    """
    A person who signs in with a username and password: a supervisor or an
    admin. Deliberately NOT the janitor.

    Why two different identity systems in one codebase:

      * The janitor's phone is a paired DEVICE (see Device above). No account,
        no password, no login screen - they type a code once and the phone
        remembers a token. CLAUDE.md forbids per-person accounts for
        janitorial staff, because an account turns logged volume into a
        performance measure attached to a named human, and the moment that
        happens people log less and the data dies.

      * A supervisor or admin is a PERSON with real authority - they can edit
        calibration, declare purity, create other accounts. That needs a real
        credential that can be revoked, not a six-character code shared around
        a staging area.

    This model is a thin badge attached to Django's built-in User. Django
    already handles the password hashing, the session and the login form; all
    we add is "which of our two roles is this, and at which site".
    """
    ROLES = [
        ("supervisor", "Supervisor"),   # monitors: dashboard, report, purity
        ("admin", "Admin"),             # the above, plus creates accounts
    ]

    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE, related_name="staff")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="staff")
    role = models.CharField(max_length=20, choices=ROLES, default="supervisor")

    # Who created this account, for the audit trail. Kept as plain text rather
    # than a foreign key so deleting the creator never erases the record of
    # who granted access.
    created_by = models.CharField(max_length=80, blank=True)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "staff"

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return self.role == "admin"
