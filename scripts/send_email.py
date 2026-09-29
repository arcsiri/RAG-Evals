import os
import smtplib
import argparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

def send_email(to_email, subject, body):
    load_dotenv()

    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = os.environ.get('SMTP_PORT')
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    email_from = os.environ.get('EMAIL_FROM')

    if not all([smtp_host, smtp_port, smtp_user, smtp_pass, email_from]):
        raise EnvironmentError("Missing SMTP credentials in .env file")

    msg = MIMEMultipart()
    msg['From'] = email_from
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--to', required=True)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--body', required=True)
    args = parser.parse_args()

    try:
        send_email(args.to, args.subject, args.body)
        print("Email sent successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")
        exit(1)
