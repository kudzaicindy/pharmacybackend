"""
Copy Django data from legacy SQLite into MongoDB (``default``).

With DJANGO_USE_MONGODB=true, Django models use MongoDB field names (e.g. ``_id``).
SQLite still uses ``id``, so we read the SQLite file with raw ``sqlite3``, not
``.using('legacy')`` on contrib / AutoField models.

Requires in .env:
  DJANGO_USE_MONGODB=true
  MONGODB_URI=...
  LEGACY_SQLITE_PATH=db.sqlite3

Run: python manage.py migrate
Then: python manage.py import_sqlite_to_mongodb --clear
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_date, parse_datetime

from chatbot.models import (
    ChatConversation,
    ChatMessage,
    MedicineRequest,
    Pharmacy,
    Pharmacist,
    PharmacyInventory,
    PharmacyRating,
    PharmacyResponse,
    PharmacistDecline,
    PatientNotification,
    PatientProfile,
    Reservation,
    SavedMedicine,
)


def _legacy_db_path():
    return str(settings.DATABASES['legacy']['NAME'])


def _open_legacy():
    # PARSE_DECLTYPES helps SQLite return datetime objects for TIMESTAMP/DATETIME columns.
    conn = sqlite3.connect(
        _legacy_db_path(),
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _uuid_val(v):
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    if isinstance(v, bytes):
        return uuid.UUID(bytes=v)
    return uuid.UUID(str(v))


def _json_val(v):
    if v is None or v == '':
        return []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return []


def _row_dict(row):
    return {k: row[k] for k in row.keys()}


def _aware_dt(value):
    """Normalize SQLite values to timezone-aware datetimes (UTC) for USE_TZ."""
    if value is None or value == '':
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except Exception:
            return None
    if isinstance(value, str):
        dt = parse_datetime(value)
        if dt is None:
            d = parse_date(value)
            if d is None:
                return None
            dt = datetime.combine(d, datetime.min.time())
        value = dt
    if not isinstance(value, datetime):
        return value
    if dj_timezone.is_aware(value):
        return value
    return dj_timezone.make_aware(value, dt_timezone.utc)


def _has_column(conn, table, column):
    info = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return any(col[1] == column for col in info)


def _table_exists(conn, name):
    row = conn.execute(
        'SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
        ('table', name),
    ).fetchone()
    return row is not None


def _copy_timestamps_from_dict(legacy_dict, saved, Model):
    updates = {}
    for f in Model._meta.fields:
        if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
            if f.attname in legacy_dict and legacy_dict[f.attname] is not None:
                updates[f.attname] = _aware_dt(legacy_dict[f.attname])
    if updates:
        Model.objects.using('default').filter(pk=saved.pk).update(**updates)


def _copy_timestamps(legacy_obj, saved, Model):
    updates = {}
    for f in Model._meta.fields:
        if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
            updates[f.attname] = _aware_dt(getattr(legacy_obj, f.attname))
    if updates:
        Model.objects.using('default').filter(pk=saved.pk).update(**updates)


class Command(BaseCommand):
    help = 'Import data from SQLite (LEGACY_SQLITE_PATH) into MongoDB default database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Wipe the MongoDB default database (flush) before import.',
        )
        parser.add_argument(
            '--skip-auth',
            action='store_true',
            help='Do not import users/groups/permissions/contenttypes (only chatbot app data).',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'DJANGO_USE_MONGODB', False):
            raise CommandError('Set DJANGO_USE_MONGODB=true to target MongoDB.')
        if 'legacy' not in settings.DATABASES:
            raise CommandError(
                'Legacy SQLite not configured. Add to .env e.g.\n'
                '  LEGACY_SQLITE_PATH=db.sqlite3\n'
                'while DJANGO_USE_MONGODB=true, then restart and run again.'
            )

        try:
            conn = _open_legacy()
        except sqlite3.Error as e:
            raise CommandError(f'Cannot open legacy SQLite: {e}') from e

        if options['clear']:
            self.stdout.write(self.style.WARNING('Flushing MongoDB default database...'))
            from django.core.management import call_command
            call_command('flush', database='default', interactive=False, verbosity=0)
        else:
            if Pharmacy.objects.using('default').exists():
                raise CommandError(
                    'MongoDB already contains data. Re-run with --clear to replace it, '
                    'or use an empty database.'
                )

        skip_auth = options['skip_auth']
        ct_map = {}
        perm_map = {}
        group_map = {}
        user_map = {}

        if not skip_auth:
            self.stdout.write('Importing contenttypes (raw SQLite)...')
            for row in conn.execute(
                'SELECT id, app_label, model FROM django_content_type ORDER BY id'
            ):
                new_ct, _ = ContentType.objects.using('default').get_or_create(
                    app_label=row['app_label'],
                    model=row['model'],
                )
                ct_map[row['id']] = new_ct

            self.stdout.write('Importing permissions (raw SQLite)...')
            for row in conn.execute(
                'SELECT id, name, content_type_id, codename FROM auth_permission ORDER BY id'
            ):
                c = ct_map[row['content_type_id']]
                new_p, _ = Permission.objects.using('default').get_or_create(
                    codename=row['codename'],
                    content_type=c,
                    defaults={'name': row['name']},
                )
                perm_map[row['id']] = new_p

            self.stdout.write('Importing groups (raw SQLite)...')
            for row in conn.execute('SELECT id, name FROM auth_group ORDER BY id'):
                new_g, _ = Group.objects.using('default').get_or_create(name=row['name'])
                group_map[row['id']] = new_g

            if _table_exists(conn, 'auth_group_permissions'):
                for row in conn.execute(
                    'SELECT group_id, permission_id FROM auth_group_permissions'
                ):
                    g = group_map[row['group_id']]
                    p = perm_map[row['permission_id']]
                    g.permissions.add(p)

            self.stdout.write('Importing users (raw SQLite)...')
            for row in conn.execute('SELECT * FROM auth_user ORDER BY id'):
                rd = _row_dict(row)
                new_u = User(
                    username=rd['username'],
                    first_name=rd.get('first_name') or '',
                    last_name=rd.get('last_name') or '',
                    email=rd.get('email') or '',
                    is_staff=bool(rd.get('is_staff')),
                    is_active=bool(rd.get('is_active')),
                    is_superuser=bool(rd.get('is_superuser')),
                    last_login=_aware_dt(rd.get('last_login')),
                    date_joined=_aware_dt(rd.get('date_joined')),
                )
                new_u.password = rd['password']
                new_u.save(using='default')
                user_map[row['id']] = new_u

            if _table_exists(conn, 'auth_user_groups'):
                for row in conn.execute('SELECT user_id, group_id FROM auth_user_groups'):
                    user_map[row['user_id']].groups.add(group_map[row['group_id']])

            if _table_exists(conn, 'auth_user_user_permissions'):
                for row in conn.execute(
                    'SELECT user_id, permission_id FROM auth_user_user_permissions'
                ):
                    user_map[row['user_id']].user_permissions.add(perm_map[row['permission_id']])
        else:
            self.stdout.write(
                self.style.WARNING(
                    '--skip-auth: user FOREIGN KEYs will be NULL unless you import auth first.'
                )
            )

        def umap(user_id):
            if user_id is None:
                return None
            return user_map.get(user_id)

        self.stdout.write('Importing pharmacies...')
        for row in Pharmacy.objects.using('legacy').order_by('pharmacy_id'):
            Pharmacy.objects.using('default').update_or_create(
                pharmacy_id=row.pharmacy_id,
                defaults={
                    'name': row.name,
                    'address': row.address,
                    'latitude': row.latitude,
                    'longitude': row.longitude,
                    'phone': row.phone,
                    'email': row.email,
                    'is_active': row.is_active,
                    'rating': row.rating,
                    'rating_count': row.rating_count,
                    'response_rate': row.response_rate,
                },
            )
        for row in Pharmacy.objects.using('legacy'):
            obj = Pharmacy.objects.using('default').get(pharmacy_id=row.pharmacy_id)
            _copy_timestamps(row, obj, Pharmacy)

        self.stdout.write('Importing chat conversations...')
        for row in ChatConversation.objects.using('legacy').order_by('conversation_id'):
            obj = ChatConversation.objects.using('default').create(
                conversation_id=row.conversation_id,
                user=umap(row.user_id),
                session_id=row.session_id,
                status=row.status,
                context_metadata=row.context_metadata,
            )
            _copy_timestamps(row, obj, ChatConversation)

        self.stdout.write('Importing chat messages...')
        for row in ChatMessage.objects.using('legacy').order_by('message_id'):
            conv = ChatConversation.objects.using('default').get(conversation_id=row.conversation_id)
            obj = ChatMessage.objects.using('default').create(
                message_id=row.message_id,
                conversation=conv,
                role=row.role,
                content=row.content,
                metadata=row.metadata,
            )
            _copy_timestamps(row, obj, ChatMessage)

        self.stdout.write('Importing medicine requests...')
        for row in MedicineRequest.objects.using('legacy').order_by('request_id'):
            conv = ChatConversation.objects.using('default').get(conversation_id=row.conversation_id)
            obj = MedicineRequest.objects.using('default').create(
                request_id=row.request_id,
                conversation=conv,
                user=umap(row.user_id),
                request_type=row.request_type,
                medicine_names=row.medicine_names,
                symptoms=row.symptoms or '',
                location_latitude=row.location_latitude,
                location_longitude=row.location_longitude,
                location_address=row.location_address or '',
                location_suburb=row.location_suburb or '',
                status=row.status,
                expires_at=_aware_dt(row.expires_at),
            )
            _copy_timestamps(row, obj, MedicineRequest)

        self.stdout.write('Importing pharmacists...')
        for row in Pharmacist.objects.using('legacy').order_by('pharmacist_id'):
            ph = Pharmacy.objects.using('default').get(pharmacy_id=row.pharmacy_id)
            obj = Pharmacist.objects.using('default').create(
                pharmacist_id=row.pharmacist_id,
                pharmacy=ph,
                user=umap(row.user_id) if row.user_id else None,
                first_name=row.first_name,
                last_name=row.last_name,
                email=row.email,
                phone=row.phone or '',
                license_number=row.license_number or '',
                is_active=row.is_active,
            )
            _copy_timestamps(row, obj, Pharmacist)

        self.stdout.write('Importing pharmacy responses...')
        for row in PharmacyResponse.objects.using('legacy').order_by('response_id'):
            req = MedicineRequest.objects.using('default').get(request_id=row.request_id)
            pharmacy = (
                Pharmacy.objects.using('default').get(pharmacy_id=row.pharmacy_id)
                if row.pharmacy_id
                else None
            )
            pharmacist = (
                Pharmacist.objects.using('default').get(pharmacist_id=row.pharmacist_id)
                if row.pharmacist_id
                else None
            )
            obj = PharmacyResponse.objects.using('default').create(
                response_id=row.response_id,
                request=req,
                pharmacy=pharmacy,
                pharmacist=pharmacist,
                pharmacy_name=row.pharmacy_name or '',
                pharmacist_name=row.pharmacist_name or '',
                medicine_available=row.medicine_available,
                price=row.price,
                preparation_time=row.preparation_time,
                distance_km=row.distance_km,
                estimated_travel_time=row.estimated_travel_time,
                medicine_responses=row.medicine_responses,
                quantity=row.quantity,
                expiry_date=row.expiry_date,
                alternative_medicines=row.alternative_medicines,
                notes=row.notes or '',
            )
            _copy_timestamps(row, obj, PharmacyResponse)

        self.stdout.write('Importing pharmacist declines (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_pharmacistdecline'):
            self.stdout.write('  (no chatbot_pharmacistdecline table; skipping)')
        else:
            for row in conn.execute(
                'SELECT request_id, pharmacist_id, declined_at, reason FROM chatbot_pharmacistdecline'
            ):
                req = MedicineRequest.objects.using('default').get(request_id=_uuid_val(row['request_id']))
                ph = Pharmacist.objects.using('default').get(pharmacist_id=_uuid_val(row['pharmacist_id']))
                rd = _row_dict(row)
                obj, _ = PharmacistDecline.objects.using('default').get_or_create(
                    request=req,
                    pharmacist=ph,
                    defaults={'reason': rd.get('reason') or ''},
                )
                _copy_timestamps_from_dict(rd, obj, PharmacistDecline)

        self.stdout.write('Importing inventory (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_pharmacyinventory'):
            self.stdout.write('  (no chatbot_pharmacyinventory table; skipping)')
        else:
            for row in conn.execute(
                '''SELECT pharmacy_id, medicine_name, quantity, reserved_quantity,
                   low_stock_threshold, price FROM chatbot_pharmacyinventory'''
            ):
                ph = Pharmacy.objects.using('default').get(pharmacy_id=row['pharmacy_id'])
                PharmacyInventory.objects.using('default').update_or_create(
                    pharmacy=ph,
                    medicine_name=row['medicine_name'],
                    defaults={
                        'quantity': row['quantity'],
                        'reserved_quantity': row['reserved_quantity'],
                        'low_stock_threshold': row['low_stock_threshold'],
                        'price': row['price'],
                    },
                )

        if not _table_exists(conn, 'chatbot_reservation'):
            self.stdout.write('  (no chatbot_reservation table; skipping reservations)')
            res_has_patient_name = False
        else:
            res_has_patient_name = _has_column(conn, 'chatbot_reservation', 'patient_name')
            res_sql = '''SELECT pharmacy_id, conversation_id, user_id, session_id,
                patient_phone, medicine_name, quantity, price_at_reservation, status,
                reserved_at, expires_at, confirmed_at, picked_up_at, cancelled_at, reservation_id'''
            if res_has_patient_name:
                res_sql += ', patient_name'
            res_sql += ' FROM chatbot_reservation'

            self.stdout.write('Importing reservations (raw SQLite)...')
            for row in conn.execute(res_sql):
                rd = _row_dict(row)
                ph = Pharmacy.objects.using('default').get(pharmacy_id=rd['pharmacy_id'])
                conv = None
                if rd.get('conversation_id'):
                    conv = ChatConversation.objects.using('default').get(
                        conversation_id=_uuid_val(rd['conversation_id'])
                    )
                patient_name = ''
                if res_has_patient_name and rd.get('patient_name'):
                    patient_name = rd['patient_name']
                obj = Reservation.objects.using('default').create(
                    reservation_id=_uuid_val(rd['reservation_id']),
                    pharmacy=ph,
                    conversation=conv,
                    user=umap(rd['user_id']) if rd.get('user_id') else None,
                    session_id=rd.get('session_id') or '',
                    patient_name=patient_name,
                    patient_phone=rd.get('patient_phone') or '',
                    medicine_name=rd['medicine_name'],
                    quantity=rd['quantity'],
                    price_at_reservation=rd.get('price_at_reservation'),
                    status=rd['status'],
                    expires_at=_aware_dt(rd['expires_at']),
                    confirmed_at=_aware_dt(rd.get('confirmed_at')),
                    picked_up_at=_aware_dt(rd.get('picked_up_at')),
                    cancelled_at=_aware_dt(rd.get('cancelled_at')),
                )
                _copy_timestamps_from_dict(rd, obj, Reservation)

        self.stdout.write('Importing pharmacy ratings (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_pharmacyrating'):
            self.stdout.write('  (no chatbot_pharmacyrating table; skipping)')
        else:
            for row in conn.execute(
                '''SELECT pharmacy_id, response_id, rating, notes, created_at
                   FROM chatbot_pharmacyrating'''
            ):
                rd = _row_dict(row)
                ph = Pharmacy.objects.using('default').get(pharmacy_id=rd['pharmacy_id'])
                resp = None
                if rd.get('response_id'):
                    resp = PharmacyResponse.objects.using('default').get(
                        response_id=_uuid_val(rd['response_id'])
                    )
                obj = PharmacyRating.objects.using('default').create(
                    pharmacy=ph,
                    response=resp,
                    rating=rd['rating'],
                    notes=rd.get('notes') or '',
                )
                _copy_timestamps_from_dict(rd, obj, PharmacyRating)

        self.stdout.write('Importing patient profiles (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_patientprofile'):
            self.stdout.write('  (no chatbot_patientprofile table; skipping)')
        else:
            for row in conn.execute('SELECT * FROM chatbot_patientprofile ORDER BY id'):
                rd = _row_dict(row)
                obj = PatientProfile.objects.using('default').create(
                    session_id=rd.get('session_id'),
                    user=umap(rd['user_id']) if rd.get('user_id') else None,
                    display_name=rd.get('display_name') or '',
                    email=rd.get('email') or '',
                    phone=rd.get('phone') or '',
                    date_of_birth=rd.get('date_of_birth'),
                    home_area=rd.get('home_area') or '',
                    preferred_language=rd.get('preferred_language') or 'en',
                    allergies=_json_val(rd.get('allergies')),
                    conditions=_json_val(rd.get('conditions')),
                    max_search_radius_km=rd.get('max_search_radius_km'),
                    sort_results_by=rd.get('sort_results_by') or 'best_match',
                    notify_pharmacy_responses=bool(rd.get('notify_pharmacy_responses', True)),
                    notify_request_expiry=bool(rd.get('notify_request_expiry', True)),
                    notify_drug_interactions=bool(rd.get('notify_drug_interactions', True)),
                    notify_medibot_followup=bool(rd.get('notify_medibot_followup', False)),
                    notification_method=rd.get('notification_method') or 'in_app',
                    share_location_with_pharmacies=bool(rd.get('share_location_with_pharmacies', True)),
                    save_search_history=bool(rd.get('save_search_history', True)),
                )
                _copy_timestamps_from_dict(rd, obj, PatientProfile)

        self.stdout.write('Importing saved medicines (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_savedmedicine'):
            self.stdout.write('  (no chatbot_savedmedicine table; skipping)')
        else:
            for row in conn.execute('SELECT * FROM chatbot_savedmedicine ORDER BY id'):
                rd = _row_dict(row)
                obj = SavedMedicine.objects.using('default').create(
                    session_id=rd['session_id'],
                    user=umap(rd['user_id']) if rd.get('user_id') else None,
                    medicine_name=rd['medicine_name'],
                    display_name=rd.get('display_name') or '',
                    last_searched_at=_aware_dt(rd.get('last_searched_at')),
                )
                _copy_timestamps_from_dict(rd, obj, SavedMedicine)

        self.stdout.write('Importing patient notifications (raw SQLite)...')
        if not _table_exists(conn, 'chatbot_patientnotification'):
            self.stdout.write('  (no chatbot_patientnotification table; skipping)')
        else:
            for row in conn.execute('SELECT * FROM chatbot_patientnotification ORDER BY id'):
                rd = _row_dict(row)
                rid = rd.get('related_request_id')
                rrid = rd.get('related_response_id')
                obj = PatientNotification.objects.using('default').create(
                    session_id=rd['session_id'],
                    user=umap(rd['user_id']) if rd.get('user_id') else None,
                    notification_type=rd.get('notification_type') or 'system',
                    title=rd['title'],
                    body=rd.get('body') or '',
                    related_request_id=_uuid_val(rid) if rid else None,
                    related_response_id=_uuid_val(rrid) if rrid else None,
                    read_at=_aware_dt(rd.get('read_at')),
                )
                _copy_timestamps_from_dict(rd, obj, PatientNotification)

        conn.close()
        self.stdout.write(self.style.SUCCESS('Import finished.'))
