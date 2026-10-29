"""Offline checks for the bounded live supplement helper; never calls a provider."""
import math
from types import SimpleNamespace

from shapely.geometry import Point
from shapely.ops import unary_union

from resume45_20261021 import cache_identity, subdivisions, cached_center_route
from app.schemas.common import CenterPoint
from app.schemas.routing import RouteResult, RoutingRequest


def test_subdivision_covers_whole_parent_disk():
    center = CenterPoint(lng=121.50860201413879, lat=31.28826762993392, crs='BD09LL')
    radius = 2504
    disks = []
    for child, child_radius in subdivisions(center, radius):
        x = math.radians(child.lng-center.lng)*6371008.8*math.cos(math.radians(center.lat))
        y = math.radians(child.lat-center.lat)*6371008.8
        disks.append(Point(x,y).buffer(child_radius, resolution=128))
    assert unary_union(disks).covers(Point(0,0).buffer(radius, resolution=128))


def test_cache_identity_excludes_keys_and_preserves_query():
    first='https://api.map.baidu.com/place/v2/search?query=test&ak=FIRST&page_num=0'
    second='https://api.map.baidu.com/place/v2/search?page_num=0&ak=SECOND&query=test'
    changed='https://api.map.baidu.com/place/v2/search?page_num=1&ak=SECOND&query=test'
    assert cache_identity(first)[0] == cache_identity(second)[0]
    assert cache_identity(first)[0] != cache_identity(changed)[0]


def test_known_center_route_requires_matching_origin_and_target_location():
    center=CenterPoint(lng=121.5,lat=31.2,crs='BD09LL')
    target=CenterPoint(lng=121.501,lat=31.2,crs='BD09LL')
    old=SimpleNamespace(center=center,routes=[RouteResult(target_id='school',location=target,status='success',distance_m=100,duration_s=90)])
    def no_network(request):
        raise AssertionError('live route must not run for unchanged evidence')
    route,reused=cached_center_route(old,no_network)
    request=RoutingRequest(schema_version='1.0',origin=center,travel_mode='walking',targets=[dict(target_id='school',location=target)])
    result=route(request).root
    assert result.ok and result.data.routes[0].duration_s==90 and reused=={'school'}
    for changed in (request.model_copy(update={'origin':target}),
                    request.model_copy(update={'targets':[request.targets[0].model_copy(update={'location':center})]})):
        try:
            route(changed)
        except AssertionError:
            pass
        else:
            raise AssertionError('changed origin or destination must request new evidence')
