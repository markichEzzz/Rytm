from django.core.management.base import BaseCommand
from django.db import transaction
from tour_routes.models import TourRoute, TourRouteStop

# Neutral development descriptions: coordinates identify places, no historical claims.
DEMOS=[('classic','Львівська класика','culture',120,[('Оперний театр',49.8440,24.0263),('Площа Ринок',49.8418,24.0316),('Вірменський квартал',49.8436,24.0308)]),
       ('green','Зелений Львів','nature',90,[('Стрийський парк',49.8227,24.0278),('Парк імені Івана Франка',49.8380,24.0202)]),
       ('coffee','Кава та старе місто','gastronomy',75,[('Площа Ринок',49.8418,24.0316),('Вірменська вулиця',49.8436,24.0308)]),
       ('unusual','Незвичайні місця Львова','hidden_gems',100,[('Підзамче',49.8502,24.0358),('Високий Замок',49.8483,24.0393)])]

class Command(BaseCommand):
    help='Create missing demo Lviv routes as drafts; never overwrites curated records.'
    @transaction.atomic
    def handle(self,*args,**options):
        created=0
        for key,title,kind,duration,stops in DEMOS:
            route,new=TourRoute.objects.get_or_create(demo_key='lviv-demo-'+key,defaults=dict(title=title,route_type=kind,estimated_duration_minutes=duration,short_description='Демонстраційний маршрут для локальної розробки.',description='Демо: орієнтовні зупинки для перевірки інтерфейсу. Перевірте координати, доступність і тривалість перед публікацією.'))
            if not new: continue
            for index,(name,lat,lon) in enumerate(stops,1):
                TourRouteStop.objects.create(route=route,order=index,title=name,latitude=lat,longitude=lon,description='Демонстраційна зупинка. Інформація для відвідування потребує перевірки.')
            created+=1
        self.stdout.write(f'Created: {created} draft routes. Existing routes were left unchanged; review new drafts in admin before publishing.')
