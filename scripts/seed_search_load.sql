-- Synthetic load data for search/availability performance checks. LOCAL USE ONLY.
-- psql -v doctors=5000 -f scripts/seed_search_load.sql
\set doctors 5000
BEGIN;
WITH new_users AS (
    INSERT INTO users (id, email, password_hash, role, mfa_enabled)
    SELECT gen_random_uuid(), 'loadtest-doctor-' || g || '@example.invalid', 'x', 'doctor', true
    FROM generate_series(1, :doctors) g
    RETURNING id, email
)
INSERT INTO doctors (user_id, display_name, registration_number, registration_council, qualification,
                     years_experience, specialties, languages, fee_paise, bio, verification_status,
                     is_accepting, rating_avg, rating_count)
SELECT id,
       (ARRAY['Asha','Bharat','Chitra','Dev','Esha','Farhan','Gauri','Harsh','Ira','Jai'])[1 + (n % 10)]
         || ' ' || (ARRAY['Sharma','Iyer','Patel','Nair','Reddy','Das','Joshi','Khan'])[1 + (n % 8)],
       'LOAD-' || n, 'NCISM', 'BAMS', n % 30,
       ARRAY[(ARRAY['kayachikitsa','panchakarma','skin_hair','digestive_health','womens_health',
                    'joint_bone','stress_sleep','diabetes_metabolic'])[1 + (n % 8)],
             (ARRAY['weight_management','respiratory','lifestyle'])[1 + (n % 3)]],
       ARRAY['en', (ARRAY['hi','ta','te','mr','bn'])[1 + (n % 5)]],
       20000 + (n % 20) * 5000,
       'Ayurveda practitioner for chronic and lifestyle conditions.',
       CASE WHEN n % 10 = 0 THEN 'pending' ELSE 'verified' END,
       n % 7 <> 0,
       round((3 + (n % 200) / 100.0)::numeric, 2), n % 500
FROM (SELECT id, row_number() OVER () AS n FROM new_users) u;

-- 60 half-hour slots per doctor over the next 10 days.
INSERT INTO availability_slots (id, doctor_id, start_at, end_at, mode, status)
SELECT gen_random_uuid(), d.user_id,
       date_trunc('hour', now()) + interval '1 hour' + (s * interval '4 hours') + ((hashtext(d.user_id::text) & 3) * interval '30 minutes'),
       date_trunc('hour', now()) + interval '1 hour' + (s * interval '4 hours') + ((hashtext(d.user_id::text) & 3) * interval '30 minutes') + interval '30 minutes',
       'video', CASE WHEN s % 4 = 0 THEN 'booked' ELSE 'open' END
FROM doctors d CROSS JOIN generate_series(0, 59) s
WHERE d.registration_number LIKE 'LOAD-%';
COMMIT;
ANALYZE doctors;
ANALYZE availability_slots;
SELECT (SELECT count(*) FROM doctors) AS doctors, (SELECT count(*) FROM availability_slots) AS slots;
