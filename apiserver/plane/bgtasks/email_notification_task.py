from datetime import datetime
from bs4 import BeautifulSoup

# Third party imports
from celery import shared_task
from sentry_sdk import capture_exception

# Django imports
from django.utils import timezone
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags

# Module imports
from plane.db.models import EmailNotificationLog, User, Issue
from plane.license.utils.instance_value import get_email_configuration
from plane.settings.redis import redis_instance

# acquire and delete redis lock
def acquire_lock(lock_id, expire_time=300):
    redis_client = redis_instance()
    """Attempt to acquire a lock with a specified expiration time."""
    return redis_client.set(lock_id, 'true', nx=True, ex=expire_time)

def release_lock(lock_id):
    """Release a lock."""
    redis_client = redis_instance()
    redis_client.delete(lock_id)

@shared_task
def stack_email_notification():
    # get all email notifications
    email_notifications = (
        EmailNotificationLog.objects.filter(processed_at__isnull=True)
        .order_by("receiver")
        .values()
    )

    # Create the below format for each of the issues
    # {"issue_id" : { "actor_id1": [ { data }, { data } ], "actor_id2": [ { data }, { data } ] }}

    # Convert to unique receivers list
    receivers = list(
        set(
            [
                str(notification.get("receiver_id"))
                for notification in email_notifications
            ]
        )
    )
    processed_notifications = []
    # Loop through all the issues to create the emails
    for receiver_id in receivers:
        # Notification triggered for the receiver
        receiver_notifications = [
            notification
            for notification in email_notifications
            if str(notification.get("receiver_id")) == receiver_id
        ]
        # create payload for all issues
        payload = {}
        email_notification_ids = []
        for receiver_notification in receiver_notifications:
            payload.setdefault(
                receiver_notification.get("entity_identifier"), {}
            ).setdefault(
                str(receiver_notification.get("triggered_by_id")), []
            ).append(
                receiver_notification.get("data")
            )
            # append processed notifications
            processed_notifications.append(receiver_notification.get("id"))
            email_notification_ids.append(receiver_notification.get("id"))

        # Create emails for all the issues
        for issue_id, notification_data in payload.items():
            send_email_notification.delay(
                issue_id=issue_id,
                notification_data=notification_data,
                receiver_id=receiver_id,
                email_notification_ids=email_notification_ids,
            )

    # Update the email notification log
    EmailNotificationLog.objects.filter(pk__in=processed_notifications).update(
        processed_at=timezone.now()
    )


def create_payload(notification_data):
    # return format {"actor_id":  { "key": { "old_value": [], "new_value": [] } }}
    data = {}
    for actor_id, changes in notification_data.items():
        for change in changes:
            issue_activity = change.get("issue_activity")
            if issue_activity:  # Ensure issue_activity is not None
                field = issue_activity.get("field")
                old_value = str(issue_activity.get("old_value"))
                new_value = str(issue_activity.get("new_value"))

                # Append old_value if it's not empty and not already in the list
                if old_value:
                    data.setdefault(actor_id, {}).setdefault(
                        field, {}
                    ).setdefault("old_value", []).append(
                        old_value
                    ) if old_value not in data.setdefault(
                        actor_id, {}
                    ).setdefault(
                        field, {}
                    ).get(
                        "old_value", []
                    ) else None

                # Append new_value if it's not empty and not already in the list
                if new_value:
                    data.setdefault(actor_id, {}).setdefault(
                        field, {}
                    ).setdefault("new_value", []).append(
                        new_value
                    ) if new_value not in data.setdefault(
                        actor_id, {}
                    ).setdefault(
                        field, {}
                    ).get(
                        "new_value", []
                    ) else None

                if not data.get("actor_id", {}).get("activity_time", False):
                    data[actor_id]["activity_time"] = str(
                        datetime.fromisoformat(
                            issue_activity.get("activity_time").rstrip("Z")
                        ).strftime("%Y-%m-%d %H:%M:%S")
                    )

    return data

def process_mention(mention_component):
    soup = BeautifulSoup(mention_component, 'html.parser')
    mentions = soup.find_all('mention-component')
    for mention in mentions:
        user_id = mention['id']
        user = User.objects.get(pk=user_id)
        user_name = user.display_name
        highlighted_name = f"@{user_name}"
        mention.replace_with(highlighted_name)
    return str(soup)

def process_html_content(content):
    processed_content_list = []
    for html_content in content:
        processed_content = process_mention(html_content)
        processed_content_list.append(processed_content)  
    return processed_content_list


@shared_task
def send_email_notification(
    issue_id, notification_data, receiver_id, email_notification_ids
):
    # Convert UUIDs to a sorted, concatenated string
    sorted_ids = sorted(email_notification_ids)
    ids_str = "_".join(str(id) for id in sorted_ids)
    lock_id = f"send_email_notif_{issue_id}_{receiver_id}_{ids_str}"

    # acquire the lock for sending emails
    try:
        if acquire_lock(lock_id=lock_id):
            # get the redis instance
            ri = redis_instance()
            base_api = (ri.get(str(issue_id)).decode())
            data = create_payload(notification_data=notification_data)

            # Get email configurations
            (
                EMAIL_HOST,
                EMAIL_HOST_USER,
                EMAIL_HOST_PASSWORD,
                EMAIL_PORT,
                EMAIL_USE_TLS,
                EMAIL_FROM,
            ) = get_email_configuration()

            receiver = User.objects.get(pk=receiver_id)
            issue = Issue.objects.get(pk=issue_id)
            template_data = []
            total_changes = 0
            comments = []
            actors_involved = []
            for actor_id, changes in data.items():
                actor = User.objects.get(pk=actor_id)
                total_changes = total_changes + len(changes)
                comment = changes.pop("comment", False)
                mention = changes.pop("mention", False)
                actors_involved.append(actor_id)
                if comment:
                    comments.append(
                        {
                            "actor_comments": comment,
                            "actor_detail": {
                                "avatar_url": actor.avatar,
                                "first_name": actor.first_name,
                                "last_name": actor.last_name,
                            },
                        }
                    )
                if mention:
                    mention["new_value"] = process_html_content(mention.get("new_value"))
                    mention["old_value"] = process_html_content(mention.get("old_value"))
                    comments.append(
                        {
                            "actor_comments": mention,
                            "actor_detail": {
                                "avatar_url": actor.avatar,
                                "first_name": actor.first_name,
                                "last_name": actor.last_name,
                            },
                        }
                    )
                activity_time = changes.pop("activity_time")
                # Parse the input string into a datetime object
                formatted_time = datetime.strptime(activity_time, "%Y-%m-%d %H:%M:%S").strftime("%H:%M %p")

                if changes:
                    template_data.append(
                        {
                            "actor_detail": {
                                "avatar_url": actor.avatar,
                                "first_name": actor.first_name,
                                "last_name": actor.last_name,
                            },
                            "changes": changes,
                            "issue_details": {
                                "name": issue.name,
                                "identifier": f"{issue.project.identifier}-{issue.sequence_id}",
                            },
                            "activity_time": str(formatted_time),
                        }
                )

            summary = "Updates were made to the issue by"

            # Send the mail
            subject = f"{issue.project.identifier}-{issue.sequence_id} {issue.name}"
            context = {
                "data": template_data,
                "summary": summary,
                "actors_involved": len(set(actors_involved)),
                "issue": {
                    "issue_identifier": f"{str(issue.project.identifier)}-{str(issue.sequence_id)}",
                    "name": issue.name,
                    "issue_url": f"{base_api}/{str(issue.project.workspace.slug)}/projects/{str(issue.project.id)}/issues/{str(issue.id)}",
                },
                "receiver": {
                    "email": receiver.email,
                },
                "issue_url": f"{base_api}/{str(issue.project.workspace.slug)}/projects/{str(issue.project.id)}/issues/{str(issue.id)}",
                "project_url": f"{base_api}/{str(issue.project.workspace.slug)}/projects/{str(issue.project.id)}/issues/",
                "workspace":str(issue.project.workspace.slug),
                "project": str(issue.project.name),
                "user_preference": f"{base_api}/profile/preferences/email",
                "comments": comments,
            }
            html_content = render_to_string(
                "emails/notifications/issue-updates.html", context
            )
            text_content = strip_tags(html_content)

            try:
                connection = get_connection(
                    host=EMAIL_HOST,
                    port=int(EMAIL_PORT),
                    username=EMAIL_HOST_USER,
                    password=EMAIL_HOST_PASSWORD,
                    use_tls=EMAIL_USE_TLS == "1",
                )

                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=text_content,
                    from_email=EMAIL_FROM,
                    to=[receiver.email],
                    connection=connection,
                )
                msg.attach_alternative(html_content, "text/html")
                msg.send()

                EmailNotificationLog.objects.filter(
                    pk__in=email_notification_ids
                ).update(sent_at=timezone.now())

                # release the lock
                release_lock(lock_id=lock_id)
                return
            except Exception as e:
                capture_exception(e)
                # release the lock
                release_lock(lock_id=lock_id)
                return
        else:
            print("Duplicate task recived. Skipping...")
            return
    except (Issue.DoesNotExist, User.DoesNotExist) as e:
        release_lock(lock_id=lock_id)
        return
