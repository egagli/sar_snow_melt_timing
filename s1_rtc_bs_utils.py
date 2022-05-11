import pystac
import pystac_client
import stackstac
import math
import numpy as np
import pandas as pd
import geopandas as gpd
import hvplot.xarray
from dask.distributed import Client
import rioxarray
import os
import matplotlib.pyplot as plt
import ulmo
from datetime import datetime
import xarray as xr
import rioxarray as rxr
import warnings
import py3dep
import geopandas as gpd
import rasterio as rio
import shapely
import scipy
import contextily as ctx

def get_s1_rtc_stac(bbox_gdf,start_time='2015-01-01',end_time=datetime.today().strftime('%Y-%m-%d'),orbit_direction='all',polarization='gamma0_vv',collection='mycollection.json'):
    '''
    Returns a Sentinel-1 SAR backscatter xarray dataset using STAC data from Indigo over the given time and bounding box.

            Parameters:
                    bbox_gdf (geopandas GeoDataframe): geodataframe bounding box
                    start_time (str): start time of returned data 'YYYY-MM-DD'
                    end_time (str): end time of returned data 'YYYY-MM-DD'
                    orbit_direction (str): orbit direction of S1--can be all, ascending, or decending
                    polarization (str): SAR polarization, use gamma0_vv
                    collection (str): points to json collection, will be different for each MGRS square

            Returns:
                    scenes (xarray dataset): xarray stack of all scenes in the specified spatio-temporal window
    '''
    # GDAL environment variables for better performance
    os.environ['AWS_REGION']='us-west-2'
    os.environ['GDAL_DISABLE_READDIR_ON_OPEN']='EMPTY_DIR' 
    os.environ['AWS_NO_SIGN_REQUEST']='YES'
    
    # Load STAC ItemCollection
    stac_items = pystac.ItemCollection.from_file(collection)
    items = [item.to_dict(transform_hrefs=False) for item in stac_items]

    stack = stackstac.stack(items,dtype='float32')
    
    bounding_box_utm_gf = bbox_gdf.to_crs(stack.crs)
    xmin, ymax, xmax, ymin = bounding_box_utm_gf.bounds.values[0]
    time_slice = slice(start_time,end_time)
    
    scenes = stack.sel(band=polarization).sel(x=slice(xmin,xmax),y=slice(ymin,ymax)).sel(time=time_slice)
    
    if orbit_direction == 'all':
        scenes = scenes
    else:
        scenes = scenes.where(scenes.coords['sat:orbit_state']==orbit_direction,drop=True)
    return scenes

def get_median_ndvi(ts_ds,start_time='2020-07-30',end_time='2020-09-09'):
    '''
    Returns the median ndvi of the area covered by a given xarray dataset using Sentinel 2 imagery given a specific temporal window. Good for building an ndvi mask.

            Parameters:
                    ts_ds (xarray dataset): the area we will return the median ndvi over
                    start_time (str): start time of returned data 'YYYY-MM-DD'
                    end_time (str): end time of returned data 'YYYY-MM-DD'

            Returns:
                    frames_ndvi_compute (xarray dataset): computed ndvi median of the Sentinel 2 stack, reprojected to the same grid as the input dataset
    '''
    # go from ds to lat lon here
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    box = shapely.geometry.box(*ds_4326.rio.bounds())
    bbox_gdf = gpd.GeoDataFrame(index=[0], crs='epsg:4326', geometry=[box])
    # must be lat lot bounding box
    lower_lon, upper_lat, upper_lon, lower_lat = bbox_gdf.bounds.values[0]
    #lower_lon, upper_lat, upper_lon, lower_lat = gdf.geometry.total_bounds

    lon = (lower_lon + upper_lon)/2
    lat = (lower_lat + upper_lat)/2
    
    URL = "https://earth-search.aws.element84.com/v0"
    catalog = pystac_client.Client.open(URL)
    
    items = catalog.search(
    intersects=dict(type="Point", coordinates=[lon, lat]),
    collections=["sentinel-s2-l2a-cogs"],
    datetime=f"{start_time}/{end_time}").get_all_items()
    
    stack = stackstac.stack(items)
    bounding_box_utm_gf = bbox_gdf.to_crs(stack.crs)
    xmin, ymax, xmax, ymin = bounding_box_utm_gf.bounds.values[0]

    cloud_cover_threshold = 20
    lowcloud = stack[stack["eo:cloud_cover"] < cloud_cover_threshold]

    nir, red, = lowcloud.sel(band="B08"), lowcloud.sel(band="B04")
    ndvi = (nir-red)/(nir+red)
    
    time_slice_ndvi = slice(start_time,end_time)
    scenes_ndvi = ndvi.sel(x=slice(xmin,xmax),y=slice(ymin,ymax)).sel(time=time_slice_ndvi).median("time", keep_attrs=True)
    scenes_ndvi = scenes_ndvi.rio.write_crs(stack.rio.crs)
    frames_ndvi_compute = scenes_ndvi.rio.reproject_match(ts_ds).compute()
    return frames_ndvi_compute

def get_py3dep_dem(ts_ds):
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    bbox = ds_4326.rio.bounds()
    dem = py3dep.get_map("DEM", bbox, resolution=10, geo_crs="epsg:4326", crs="epsg:3857")
    dem.name = "dem"
    dem.attrs["units"] = "meters"
    dem_reproject = dem.rio.reproject_match(ts_ds) 
    return dem_reproject

def get_py3dep_aspect(ts_ds):
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    bbox = ds_4326.rio.bounds()
    dem = py3dep.get_map("Aspect Degrees", bbox, resolution=10, geo_crs="epsg:4326", crs="epsg:3857")
    dem.name = "aspect"
    dem.attrs["units"] = "degrees"
    dem_reproject = dem.rio.reproject_match(ts_ds)
    return dem_reproject

def get_py3dep_slope(ts_ds):
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    bbox = ds_4326.rio.bounds()
    dem = py3dep.get_map("Slope Degrees", bbox, resolution=10, geo_crs="epsg:4326", crs="epsg:3857")
    dem.name = "slope"
    dem.attrs["units"] = "degrees"
    dem_reproject = dem.rio.reproject_match(ts_ds) 
    return dem_reproject

def get_dah(ts_ds):
    # Diurnal Anisotropic Heating Index [Böhner and Antonić, 2009]
    # https://www.sciencedirect.com/science/article/abs/pii/S0166248108000081
    # DAH = cos(alpha_max-alpha)*arctan(beta) where alpha_max is slope aspect 
    # recieving maximum heating alpha is slope aspect, beta is slope aspect
    # in radians. adpated from: https://agupubs.onlinelibrary.wiley.com/doi/full/10.1002/2017WR020799
    # https://avalanche.org/avalanche-encyclopedia/aspect/
    alpha_max = 202.5
    aspect = get_py3dep_aspect(ts_ds)
    slope = get_py3dep_slope(ts_ds)
    DAH = np.cos(np.deg2rad(alpha_max-aspect))*np.arctan(np.deg2rad(slope))
    DAH_reproject = DAH.rio.reproject_match(ts_ds)
    return DAH_reproject

#def get_runoff_onset(ts_ds):
#    mins_info_runoff = ts_ds.argmin(dim='time',skipna=False)
#    runoff_dates = ts_ds[mins_info_runoff].time
#    return runoff_dates

def get_runoff_onset(ts_ds):
    ts_ds = ts_ds.fillna(9999)
    mins_info_runoff = ts_ds.argmin(dim='time',skipna=True)
    runoff_dates = ts_ds[mins_info_runoff].time
    runoff_dates = runoff_dates.where(ts_ds.isel(time=0)!=9999)
    return runoff_dates

def get_ripening_onset(ts_ds,orbit='ascending'): # fix this
    ts_ds = ts_ds.fillna(9999)
    ts_ds = ts_ds.where(ts_ds.coords['sat:orbit_state']==orbit,drop=True)
    mins_info_ripening = ts_ds.differentiate(coord='time',datetime_unit='W').argmin(dim='time',skipna=False) # dt=week
    ripening_dates = ts_ds[mins_info_ripening].time
    ripening_dates = ripening_dates.where(ts_ds.isel(time=0)!=9999)
    return ripening_dates

def get_stats(ts_ds,dem=None,aspect=None,dah=None):
    runoff_dates = get_runoff_onset(ts_ds)
    ripening_dates = get_ripening_onset(ts_ds)
    if dem is None:
        dem_projected = get_py3dep_dem(ts_ds)
    else:
        dem_projected = dem
    if aspect is None:
        aspect_projected = get_py3dep_aspect(ts_ds)
    else:
        aspect_projected = aspect
    if dah is None:
        dah_projected = get_dah(ts_ds)
    else: 
        dah_projected = dah
    dates_df = pd.DataFrame(columns=['x','y','elevation', 'aspect','aspect_rescale','dah','runoff_dates','ripening_dates'])
    a1, a2 = np.meshgrid(dem_projected.indexes['x'],dem_projected.indexes['y'])
    dates_df['x'] = a1.reshape(-1)
    dates_df['y'] = a2.reshape(-1)
    dates_df['elevation'] = dem_projected.data.reshape(-1)
    dates_df['aspect'] = aspect_projected.data.reshape(-1)
    dates_df['aspect_rescale'] = np.abs(aspect_projected.data.reshape(-1)-180)
    dates_df['dah'] = dah_projected.data.reshape(-1)
    dates_df['runoff_dates'] = runoff_dates.dt.dayofyear.data.reshape(-1)
    dates_df['ripening_dates'] = ripening_dates.dt.dayofyear.data.reshape(-1)
    dates_df = dates_df.dropna()
    
    dates_mls_df = dates_df.filter(['elevation','dah','runoff_dates','ripening_dates'])
    
    predictors = np.append(np.ones_like([dates_df['runoff_dates']]).T,dates_mls_df.iloc[:,[0,1]].to_numpy(),axis=1)
    B,_,_,_ = scipy.linalg.lstsq(predictors, dates_mls_df.iloc[:,2])
    dates_df['runoff_prediction'] = predictors.dot(B)
    
    predictors = np.append(np.ones_like([dates_df['runoff_dates']]).T,dates_mls_df.iloc[:,[0,1]].to_numpy(),axis=1)
    B,_,_,_ = scipy.linalg.lstsq(predictors, dates_mls_df.iloc[:,3])
    dates_df['ripening_prediction'] = predictors.dot(B)
    
    dates_gdf = gpd.GeoDataFrame(dates_df,geometry=gpd.points_from_xy(dates_df['x'],dates_df['y'],crs=ts_ds.rio.crs))
    dates_gdf=dates_gdf.set_index(['y','x'])
    
    return dates_gdf



def plot_timeseries_by_elevation_bin(ts_ds,dem_ds,bin_size=100,ax=None,normalize_bins=False):
    if ax is None:
        ax = plt.gca()
    f = plt.gcf()
    
    dem_projected_ds = dem_ds.rio.reproject_match(ts_ds) # squeeze??
    dem_projected_ds = dem_projected_ds.where(ts_ds!=np.nan) # here mask DEM by ts_ds
    
    bin_centers=list(range(int(math.floor(dem_projected_ds.max()/100)*100)-bin_size//2,int(math.ceil(dem_projected_ds.min()/100)*100),-bin_size))
    backscatter_full = []

    for i,bin_center in enumerate(bin_centers):
        ts_bin_ds = ts_ds.where(np.abs(dem_projected_ds - bin_center) < bin_size//2)
        with warnings.catch_warnings(): #catches np.nanmean empty slices
            warnings.simplefilter("ignore", category=RuntimeWarning)
            backscatter_ts_for_bin = np.nanmean(ts_bin_ds.data.reshape(ts_bin_ds.shape[0],-1), axis=1) 
        backscatter_full.append(list(backscatter_ts_for_bin))
        
    backscatter_df = pd.DataFrame(backscatter_full,index=bin_centers,columns=ts_ds.time)
    
    if normalize_bins == True:
          backscatter_df = ((backscatter_df.T-backscatter_df.T.min())/(backscatter_df.T.max()-backscatter_df.T.min())).T
    colors = ax.pcolormesh(pd.to_datetime(ts_ds.time), bin_centers, backscatter_df,cmap='inferno',edgecolors=(1.0, 1.0, 1.0, 0.3)) #,vmin=0,vmax=0.5
    cbar = f.colorbar(colors,ax=ax)
    
    if normalize_bins == False:
        lab = 'Mean Backscatter [Watts]'
    else:
        lab = 'Normalized (Elevation-wise) Backscatter'
    
    cbar.ax.set_ylabel(lab, rotation=270, labelpad=15)

    ax.set_xlabel('Time')
    ax.set_ylabel('Elevation [m]')
    return ax

def plot_timeseries_by_dah_bin(ts_ds,dem_ds,bin_size=0.25,ax=None,normalize_bins=False):
    if ax is None:
        ax = plt.gca()
    f = plt.gcf()
    
    dem_projected_ds = dem_ds.rio.reproject_match(ts_ds) # squeeze??
    dem_projected_ds = dem_projected_ds.where(ts_ds!=np.nan) # here mask DEM by ts_ds
    
    bin_centers=list(np.arange(-1+bin_size/2,1,bin_size))
    backscatter_full = []

    for i,bin_center in enumerate(bin_centers):
        ts_bin_ds = ts_ds.where(np.abs(dem_projected_ds - bin_center) < bin_size/2)
        with warnings.catch_warnings(): #catches np.nanmean empty slices
            warnings.simplefilter("ignore", category=RuntimeWarning)
            backscatter_ts_for_bin = np.nanmean(ts_bin_ds.data.reshape(ts_bin_ds.shape[0],-1), axis=1) 
        backscatter_full.append(list(backscatter_ts_for_bin))
        
    backscatter_df = pd.DataFrame(backscatter_full,index=bin_centers,columns=ts_ds.time)
    
    if normalize_bins == True:
          backscatter_df = ((backscatter_df.T-backscatter_df.T.min())/(backscatter_df.T.max()-backscatter_df.T.min())).T
    colors = ax.pcolormesh(bin_centers, pd.to_datetime(ts_ds.time), backscatter_df.T,cmap='inferno',edgecolors=(1.0, 1.0, 1.0, 0.3)) #,vmin=0,vmax=0.5
    cbar = f.colorbar(colors,ax=ax,location='top',orientation='horizontal')
    
    if normalize_bins == False:
        lab = 'Mean Backscatter [Watts]'
    else:
        lab = 'Normalized (DAH-wise) Backscatter'
    
    #cbar.ax.set_ylabel(lab, rotation=270, labelpad=15)
    
    ax.set_xlabel('Diurnal Anisotropic Heating Index')
    ax.set_ylabel('Time')
    return ax

def plot_hyposometry(ts_ds,dem_ds,bin_size=100,ax=None):
    if ax is None:
        ax = plt.gca()
    f = plt.gcf()    
    dem_projected_ds = dem_ds.rio.reproject_match(ts_ds) # squeeze??
    dem_projected_ds = dem_projected_ds.where(ts_ds!=np.nan) # here mask DEM by ts_ds
    bin_edges=list(range(int(math.floor(dem_projected_ds.max()/100)*100)+bin_size,int(math.ceil(dem_projected_ds.min()/100)*100)-bin_size,-bin_size))
    ax.hist(dem_projected_ds.squeeze().isel(time=0).to_numpy().ravel(),bins=bin_edges[::-1],orientation='horizontal',histtype='bar',ec='k')
    ax.set_ylim([np.array(bin_edges).min(),np.array(bin_edges).max()])
    ax.set_xlabel('# of Pixels')
    ax.set_ylabel('Elevation [m]')
    ax.set_title('Hyposometry Plot')
    return ax


def plot_dah_bins(ts_ds,dem_ds,bin_size=0.25,ax=None):
    if ax is None:
        ax = plt.gca()
    f = plt.gcf()    
    dem_projected_ds = dem_ds.rio.reproject_match(ts_ds) # squeeze??
    dem_projected_ds = dem_projected_ds.where(ts_ds!=np.nan) # here mask DEM by ts_ds
    bin_edges=list(np.arange(-1,1+bin_size,bin_size))
    ax.hist(dem_projected_ds.squeeze().isel(time=0).to_numpy().ravel(),bins=bin_edges[::1],orientation='vertical',histtype='bar',ec='k')
    ax.set_xlim([-1,1])
    ax.set_ylabel('# of Pixels')
    ax.set_xlabel('DAH')
    ax.set_title('DAH Index Histogram')
    return ax


def plot_backscatter_ts_and_ndvi(ts_ds,ndvi_ds):
    frames = ts_ds
    frames_ndvi_all = ndvi_ds
    mins_info = frames.argmin(dim='time',skipna=False)
    f,ax=plt.subplots(3,2,figsize=(20,10))
    frames[mins_info].time.dt.dayofyear.where(frames_ndvi_all.values<0.2).plot(ax=ax[0,0],cmap='twilight')
    ax[0,0].set_title('Runoff Date w/ No Vegetation \n (NDVI < 0.2)')
    frames[mins_info].time.dt.dayofyear.where(frames_ndvi_all.values>0.2).where(frames_ndvi_all.values<0.6).plot(ax=ax[1,0],cmap='twilight')
    ax[1,0].set_title('Runoff Date w/ Sparse to Moderate Vegetation \n (0.2 < NDVI < 0.6)')
    #frames[mins_info].time.dt.dayofyear.where(frames_ndvi_all.values>0.4).where(frames_ndvi_all.values<0.6).plot(ax=ax[2,0])
    #ax[2,0].set_title('Runoff Date w/ Moderate Vegetation \n (0.4 < NDVI < 0.6)')
    frames[mins_info].time.dt.dayofyear.where(frames_ndvi_all.values>0.6).plot(ax=ax[2,0],cmap='twilight')
    ax[2,0].set_title('Runoff Date w/ Dense Vegetation \n (NDVI > 0.6)')

    ax[0,0].set_aspect('equal')
    ax[1,0].set_aspect('equal')
    ax[2,0].set_aspect('equal')

    ax[0,1].plot(frames.where(frames_ndvi_all.values<0.2).time,frames.where(frames_ndvi_all.values<0.2).mean(dim=['x','y']))

    ax[1,1].plot(frames.where(frames_ndvi_all.values>0.2).where(frames_ndvi_all.values<0.6).time,frames.where(frames_ndvi_all.values>0.2).where(frames_ndvi_all.values<0.6).mean(dim=['x','y']))

    #ax[2,1].plot(frames.where(frames_ndvi_all.values>0.4).where(frames_ndvi_all.values<0.6).time,frames.where(frames_ndvi_all.values>0.4).where(frames_ndvi_all.values<0.6).mean(dim=['x','y']))

    ax[2,1].plot(frames.where(frames_ndvi_all.values>0.6).time,frames.where(frames_ndvi_all.values>0.6).mean(dim=['x','y']))

    ax[0,1].set_title('Backscatter Time Series')
    ax[1,1].set_title('Backscatter Time Series')
    ax[2,1].set_title('Backscatter Time Series')

    ax[0,1].set_ylabel('Backscatter [Watts]')
    ax[1,1].set_ylabel('Backscatter [Watts]')
    ax[2,1].set_ylabel('Backscatter [Watts]')

    ax[0,1].set_ylim([0,0.5])
    ax[1,1].set_ylim([0,0.5])
    ax[2,1].set_ylim([0,0.5])

    plt.tight_layout()
    
def find_closest_snotel(ts_ds):
    
    sites_df=pd.DataFrame.from_dict(ulmo.cuahsi.wof.get_sites('https://hydroportal.cuahsi.org/Snotel/cuahsi_1_1.asmx?WSDL'),orient='index').astype({'elevation_m': 'float'})
    locations = pd.json_normalize(sites_df['location']).astype({'latitude': 'float','longitude':'float'})
    sites_gdf = gpd.GeoDataFrame(sites_df[['code','name','elevation_m']], geometry=gpd.points_from_xy(locations.longitude, locations.latitude))
    
    sites_gdf = sites_gdf.set_crs('epsg:4326')
    sites_gdf = sites_gdf.to_crs(ts_ds.crs)
    
    sites_gdf['distance_km'] = sites_gdf.distance(shapely.geometry.box(*ts_ds.rio.bounds()))/1000
    sites_gdf = sites_gdf.sort_values(by='distance_km')
    sites_gdf = sites_gdf[sites_gdf['distance_km'].notnull()]

    return sites_gdf

def plot_closest_snotel(ts_ds,distance_cutoff=30,ax=None):
    
    if ax is None:
        ax = plt.gca()
    f = plt.gcf()    
    
    sites_gdf = find_closest_snotel(ts_ds) 
    
    ts_ds.isel(time=0).plot(ax=ax,vmax=1.0,cmap='gray',add_colorbar=False)
    sites_gdf = sites_gdf[sites_gdf['distance_km']<distance_cutoff]
    color = sites_gdf.plot(column='distance_km',ax=ax,vmax=distance_cutoff,legend=True,cmap='viridis_r',legend_kwds={'label':'Distance from Study Site [km]','orientation':'vertical','fraction':0.0466,'pad':0.02})
    minx, miny, maxx, maxy = ts_ds.rio.bounds()
    ax.set_xlim([minx-1000*distance_cutoff*1.2,maxx+1000*distance_cutoff*1.2])
    ax.set_ylim([miny-1000*distance_cutoff*1.2,maxy+1000*distance_cutoff*1.2])

    ctx.add_basemap(ax=ax, crs=sites_gdf.crs, source=ctx.providers.Stamen.Terrain)

    ax.set_title('SNOTEL Sites Around Study Site')
    plt.tight_layout(rect=[0, 0, 0.9, 0.90])

    for x, y, label1, label2, label3 in zip(sites_gdf.geometry.x, sites_gdf.geometry.y, sites_gdf.name, sites_gdf.code, sites_gdf.distance_km):
        ax.annotate(f'{label1} \n{label2} \n{label3:.2f} km', xy=(x, y), xytext=(15, -30), textcoords="offset points", fontsize=10,bbox=dict(facecolor='yellow', edgecolor='black', boxstyle='round,pad=0.5'))
    
    return ax

def get_snotel(site_code, variable_code='SNOTEL:SNWD_D', start_date='1900-01-01', end_date=datetime.today().strftime('%Y-%m-%d')):
    
    wsdlurl = 'https://hydroportal.cuahsi.org/Snotel/cuahsi_1_1.asmx?WSDL'
    #print(ulmo.cuahsi.wof.get_site_info(wsdlurl, sitecode)['series'].keys())

    #print(sitecode, variablecode, start_date, end_date)
    values_df = None
    try:
        #Request data from the server
        site_values = ulmo.cuahsi.wof.get_values(wsdlurl, site_code, variable_code, start=start_date, end=end_date)
        #Convert to a Pandas DataFrame   
        values_df = pd.DataFrame.from_dict(site_values['values'])
        #Parse the datetime values to Pandas Timestamp objects
        values_df['datetime'] = pd.to_datetime(values_df['datetime'], utc=True)
        #Set the DataFrame index to the Timestamps
        values_df = values_df.set_index('datetime')
        #Convert values to float and replace -9999 nodata values with NaN
        values_df['value'] = pd.to_numeric(values_df['value']).replace(-9999, np.nan)
        #Remove any records flagged with lower quality
        values_df = values_df[values_df['quality_control_level_code'] == '1']
    except:
        print("Unable to fetch %s" % variable_code)

    return values_df

def get_closest_snotel_data(ts_ds,variable_code='SNOTEL:SNWD_D',distance_cutoff=30,start_date='1900-01-01', end_date=datetime.today().strftime('%Y-%m-%d')):
    
    sites_df = find_closest_snotel(ts_ds)
    sites_df = sites_df[sites_df['distance_km']<distance_cutoff]
    
    values_dict = {}
    
    for site_code in sites_df['code']:
        new_site = get_snotel(f'SNOTEL:{site_code}', variable_code,start_date=start_date, end_date=end_date)
        values_dict[site_code] = new_site['value']
        
    site_data_df = pd.DataFrame.from_dict(values_dict)
    
    return site_data_df

def get_s2_ndsi(ts_ds):
    '''
    Returns the ndsi time series of the area covered by a given xarray dataset using Sentinel 2 imagery

            Parameters:
                    ts_ds (xarray dataset): the area we will return the median ndsi over

            Returns:
                    scenes_ndsi_compute (xarray dataset): computed ndsi time series with same spatial grid and temporal bounds as as the input dataset
    '''
    # go from ds to lat lon here
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    box = shapely.geometry.box(*ds_4326.rio.bounds())
    bbox_gdf = gpd.GeoDataFrame(index=[0], crs='epsg:4326', geometry=[box])
    # must be lat lot bounding box
    lower_lon, upper_lat, upper_lon, lower_lat = bbox_gdf.bounds.values[0]
    #lower_lon, upper_lat, upper_lon, lower_lat = gdf.geometry.total_bounds

    lon = (lower_lon + upper_lon)/2
    lat = (lower_lat + upper_lat)/2
    
    start_time = pd.to_datetime(ts_ds.time[0].values).strftime('%Y-%m-%d')
    end_time = pd.to_datetime(ts_ds.time[-1].values).strftime('%Y-%m-%d')
    
    URL = "https://earth-search.aws.element84.com/v0"
    catalog = pystac_client.Client.open(URL)
    
    items = catalog.search(
    intersects=dict(type="Point", coordinates=[lon, lat]),
    collections=["sentinel-s2-l2a-cogs"],
    datetime=f"{start_time}/{end_time}").get_all_items()
    
    stack = stackstac.stack(items)
    bounding_box_utm_gf = bbox_gdf.to_crs(stack.crs)
    xmin, ymax, xmax, ymin = bounding_box_utm_gf.bounds.values[0]

    cloud_cover_threshold = 20
    lowcloud = stack[stack["eo:cloud_cover"] < cloud_cover_threshold]
    lowcloud = lowcloud
    #lowcloud = lowcloud.drop_duplicates("time","first")
    # snow.groupby(snow.time.dt.date).mean() use this for groupby date
    vir, swir = lowcloud.sel(band="B03"), lowcloud.sel(band="B11")
    ndsi = (vir-swir)/(vir+swir)
    
    time_slice = slice(start_time,end_time)
    scenes_ndsi = ndsi.sel(x=slice(xmin,xmax),y=slice(ymin,ymax)).sel(time=time_slice)
    scenes_ndsi = scenes_ndsi.rio.write_crs(stack.rio.crs)
    scenes_ndsi_compute = scenes_ndsi.rio.reproject_match(ts_ds).resample(time='1D',skipna=True).mean("time", keep_attrs=True).dropna('time',how='all')#.compute()
    scenes_ndsi_compute = scenes_ndsi_compute.where(ts_ds.isel(time=0)>0)
    return scenes_ndsi_compute

def get_s2_rgb(ts_ds):
    '''
    Returns the rgb time series of the area covered by a given xarray dataset using Sentinel 2 imagery

            Parameters:
                    ts_ds (xarray dataset): the area we will return the rgb over

            Returns:
                    scenes_rgb_compute (xarray dataset): computed rgb time series with same spatial grid and temporal bounds as as the input dataset
    '''
    # go from ds to lat lon here
    ds_4326 = ts_ds.rio.reproject('EPSG:4326', resampling=rio.enums.Resampling.cubic)
    box = shapely.geometry.box(*ds_4326.rio.bounds())
    bbox_gdf = gpd.GeoDataFrame(index=[0], crs='epsg:4326', geometry=[box])
    # must be lat lot bounding box
    lower_lon, upper_lat, upper_lon, lower_lat = bbox_gdf.bounds.values[0]
    #lower_lon, upper_lat, upper_lon, lower_lat = gdf.geometry.total_bounds

    lon = (lower_lon + upper_lon)/2
    lat = (lower_lat + upper_lat)/2
    
    start_time = pd.to_datetime(ts_ds.time[0].values).strftime('%Y-%m-%d')
    end_time = pd.to_datetime(ts_ds.time[-1].values).strftime('%Y-%m-%d')
    
    URL = "https://earth-search.aws.element84.com/v0"
    catalog = pystac_client.Client.open(URL)
    
    items = catalog.search(
    intersects=dict(type="Point", coordinates=[lon, lat]),
    collections=["sentinel-s2-l2a-cogs"],
    datetime=f"{start_time}/{end_time}").get_all_items()
    
    stack = stackstac.stack(items)
    bounding_box_utm_gf = bbox_gdf.to_crs(stack.crs)
    xmin, ymax, xmax, ymin = bounding_box_utm_gf.bounds.values[0]

    cloud_cover_threshold = 20
    lowcloud = stack[stack["eo:cloud_cover"] < cloud_cover_threshold]
    lowcloud = lowcloud
    #lowcloud = lowcloud.drop_duplicates("time","first")
    # snow.groupby(snow.time.dt.date).mean() use this for groupby date
    rgb = lowcloud.sel(band=["B04","B03","B02"])
    
    time_slice = slice(start_time,end_time)
    scenes_rgb = rgb.sel(x=slice(xmin,xmax),y=slice(ymin,ymax)).sel(time=time_slice)
    scenes_rgb = scenes_rgb.rio.write_crs(stack.rio.crs)
    scenes_rgb_compute = scenes_rgb.resample(time='1W',skipna=True).mean("time", keep_attrs=True).dropna('time',how='all')#.compute()
    
    return scenes_rgb_compute