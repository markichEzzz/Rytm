from django.db.models.signals import pre_save
from django.dispatch import receiver
from firebase_admin import messaging
from .models import Event

@receiver(pre_save, sender=Event)
def send_push_on_approval(sender, instance, **kwargs):
    # Якщо це нова подія, яка ще не має ID, пропускаємо
    if not instance.pk:
        return

    try:
        # Отримуємо стару версію події з бази
        old_event = Event.objects.get(pk=instance.pk)
    except Event.DoesNotExist:
        return

    # Перевіряємо, чи статус змінився на 'approved' саме зараз
    if old_event.status != 'approved' and instance.status == 'approved':
        try:
            # Створюємо повідомлення (відправляємо на топік 'all_users' або 'lviv_events')
            message = messaging.Message(
                notification=messaging.Notification(
                    title="Нова подія: " + instance.title,
                    body=f"Категорія: {instance.get_category_display()}. Знайди на карті!",
                ),
                topic='lviv_events' # Колега в Android-додатку має підписати користувачів на цей топік
            )
            # Надсилаємо
            response = messaging.send(message)
            print("Успішно надіслано push-сповіщення:", response)
        except Exception as e:
            print("Помилка відправки push-сповіщення:", e)