import os, time, boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION","ap-south-1")
TABLE = os.environ.get("MEDTRACK_TABLE","MedTrack")
TOPIC = os.environ.get("SNS_TOPIC_NAME","MedTrackNotifications")

ddb = boto3.client("dynamodb", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)

try:
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName":"PK","KeyType":"HASH"},
                   {"AttributeName":"SK","KeyType":"RANGE"}],
        AttributeDefinitions=[{"AttributeName":"PK","AttributeType":"S"},
                              {"AttributeName":"SK","AttributeType":"S"}],
        BillingMode="PAY_PER_REQUEST"
    )
    print("DynamoDB table creation started:", TABLE)
except ClientError as e:
    if e.response["Error"]["Code"] == "ResourceInUseException":
        print("DynamoDB table already exists.")
    else:
        raise

try:
    response = sns.create_topic(Name=TOPIC)
    print("SNS topic ARN:", response["TopicArn"])
    print("Set SNS_TOPIC_ARN to this value before running the app.")
except ClientError as e:
    print("SNS error:", e)

print("AWS setup finished. Wait until DynamoDB status is ACTIVE.")
