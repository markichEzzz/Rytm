"""WGS84 geodesic lengths between ordered stops, NOT pedestrian routing."""
from django.db import connection

def attach_distances(routes):
    legs = []
    for route in routes:
        stops = list(route.stops.all())
        legs.extend((route.pk, a.longitude, a.latitude, b.longitude, b.latitude) for a, b in zip(stops, stops[1:]))
        route._distance_meters = 0.0
    if not legs: return
    placeholders = ','.join(['(%s::bigint,%s::double precision,%s::double precision,%s::double precision,%s::double precision)'] * len(legs))
    with connection.cursor() as cursor:
        cursor.execute('SELECT id, SUM(ST_Distance(ST_SetSRID(ST_MakePoint(x1,y1),4326)::geography, '
                       'ST_SetSRID(ST_MakePoint(x2,y2),4326)::geography)) '
                       'FROM (VALUES ' + placeholders + ') AS legs(id,x1,y1,x2,y2) GROUP BY id',
                       [value for leg in legs for value in leg])
        totals = dict(cursor.fetchall())
    for route in routes: route._distance_meters = round(float(totals.get(route.pk, 0)), 1)
