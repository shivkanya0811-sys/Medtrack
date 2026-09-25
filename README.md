# MedTrack - Flask + AWS

## Features
- User registration/login with doctor/patient roles
- Doctor and patient profiles
- Patient appointment booking with doctor selection
- Doctor dashboard with appointment search and status updates
- Appointment status history stored in DynamoDB
- Diagnosis reports linked one-to-one with appointments
- In-app notifications and optional SNS notifications
- Optional SES email to patient, doctor and admin
- Local in-memory storage by default for development and guided-project testing
- Optional DynamoDB storage through the EC2 IAM role
- EC2 deployment

## Configuration
The application runs locally without AWS credentials by default. Set
`MEDTRACK_USE_AWS=true` only after the Troven Labs AWS resources are ready.

For local development:
```bash
python -m venv .venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

For AWS mode, configure environment variables rather than putting credentials
in source code:

## AWS configuration
Set:
- `AWS_REGION=ap-south-1`
- `MEDTRACK_TABLE=MedTrack`
- `MEDTRACK_USE_AWS=true`
- `SNS_TOPIC_ARN` (optional)
- `FROM_EMAIL` (verified SES sender, for email)
- `ADMIN_EMAIL` (admin recipient, for email)
- `FLASK_SECRET_KEY`

For EC2, use an IAM role with DynamoDB, SNS and SES permissions rather than storing AWS keys on the server.

The current AWS implementation uses a DynamoDB single-table layout with
`Entity` values for users, profiles, appointments, diagnoses, and notifications.
`setup_aws.py` creates the table and SNS topic; it does not create credentials.

## EC2 deployment
```bash
pip install -r requirements.txt
python setup_aws.py
flask run --host=0.0.0.0 --port=5000
```
Open http://127.0.0.1:5000.

An EC2 deployment should use an IAM instance role with least-privilege
DynamoDB and SNS permissions. Do not commit AWS keys, `.env`, `.venv`, or
`__pycache__` files. SES email is optional and disabled unless configured.

## Functional testing coverage
1. Home page navigation
2. Doctor/patient registration
3. Doctor/patient login
4. Patient appointment booking
5. Doctor appointment search
6. Doctor appointment status update
7. Appointment status history
8. Diagnosis report creation and one-to-one validation
9. User/appointment/notification DynamoDB records
10. Optional email to user and admin when SES is configured
11. SNS notification publishing when configured
