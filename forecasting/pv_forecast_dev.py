#from generic_vpp_server import GenericVPPServer
#from multiprocessing import Manager
import time, sys
import os
import pvlib
import pytz
from datetime import datetime
from datetime import timedelta
import pandas as pd
import numpy as np
import statsmodels.api as sm
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import matplotlib.dates as mdates

class PVobj():
    # create an instance of a PV class object
    def __init__(self, derid, dc_capacity, ac_capacity,
                 lat, lon, alt, tz, tilt, azimuth,
                 forecast_method='ARMA', surrogateid=None, 
                 base_year=2016):
        self.derid = derid
        # TODO: using a surrogate in forecasting is NOT implemented
        self.surrogateid = surrogateid
        if forecast_method in ['ARMA']:
            self.forecast_method = forecast_method
        else:
            # TODO: raise error
            self.forecast_method = None
        self.dc_capacity = dc_capacity
        self.ac_capacity = ac_capacity
        self.lat = lat
        self.lon = lon
        self.alt = alt
        self.timezone = tz
        self.tilt = tilt
        self.azimuth = azimuth
        
        # pre-compute clear sky power
        dr = pd.DatetimeIndex(start=datetime(base_year, 1, 1, 0, 0, 0, tzinfo=tz),
                              end=datetime(base_year, 12, 31, 23, 59, 0, tzinfo=tz),
                              freq='1T')
        self.clearskypower = pd.DataFrame(index=dr, columns=['csGHI', 'csPOA', 'DCpower', 'ACpower'])
        clearSky = clear_sky_model(self, dr)
        self.clearskypower['csGHI'] = clearSky['GHI']
        self.clearskypower['csPOA'] = clearSky['POA']
        self.clearskypower['DCpower'] = self.clearskypower['csPOA']/1000*self.dc_capacity
        self.clearskypower['ACpower'] = np.where(self.clearskypower['DCpower']>self.ac_capacity, 
                                                 self.ac_capacity, self.clearskypower['DCpower'])
        
    # PVobj member functions

# Forecast functions
def solar_position(pvobj, dr):
    # returns ephemeris in a dataframe sp

    Location = pvlib.location.Location(pvobj.lat,
                                       pvobj.lon,
                                       pvobj.timezone,
                                       pvobj.alt)

    sp = pvlib.solarposition.ephemeris(dr, Location.latitude, Location.longitude)

    return sp
    
def dniDiscIrrad(ghi, zenith, dr):
    """
    Use the DISC model to estimate the DNI from GHI weather forecast
    :return: DNI
    """
    disc = pvlib.irradiance.disc(ghi=ghi, zenith=zenith, datetime_or_doy=dr)
    # NA's show up when the DNI component is zero (or less than zero), fill with 0.
    disc.fillna(0, inplace=True)

    return disc['dni']

def DHIfromGHI(ghi, dni, altitude):
    """
    Solve for DHI
    GHI =  DHI + DNI*sin(solar altitude angle)
    :return: DNI
    """
    return ghi - dni * np.sin(altitude * (np.pi / 180))

def clear_sky_model(pvobj, dr):
    # returns clear-sky model and ephemeris in dataframes clearSky and sp, respectively

    # get solar position information
    sp = solar_position(pvobj, dr)
    # initialize clear sky df and fill with information
    clearSky = pd.DataFrame(index=pd.DatetimeIndex(dr))

    clearSky['GHI'] = pvlib.clearsky.haurwitz(sp['apparent_zenith'])
    
    clearSky['DNI'] = dniDiscIrrad(ghi=clearSky['GHI'], zenith=sp['zenith'], dr=dr)
    clearSky['DHI'] = DHIfromGHI(ghi=clearSky['GHI'], dni=clearSky['DNI'], altitude=sp['elevation'])
    clearSky['ExtraI'] = pvlib.irradiance.extraradiation(dr)


    clearSky['AOI'] = pvlib.irradiance.aoi(surface_tilt=pvobj.tilt,
                                           surface_azimuth=pvobj.azimuth,
                                           solar_zenith=sp['zenith'],
                                           solar_azimuth=sp['azimuth'])
    # Convert the AOI to radians for simplicity in further trigonometric calculations
    clearSky['AOI'] = clearSky['AOI']*(np.pi/180.0)

    # Calculate the POA irradiance based on the given site information
    clearSky['BeamI'] = pvlib.irradiance.beam_component(pvobj.tilt,
                                                        pvobj.azimuth,
                                                        sp['zenith'],
                                                        sp['azimuth'],
                                                        clearSky['DNI'])

    # Calculate the diffuse radiation from the sky (using Hay and Davies)
    clearSky['DiffuseSkyI'] = pvlib.irradiance.haydavies(pvobj.tilt,
                                                         pvobj.azimuth,
                                                         clearSky['DHI'],
                                                         clearSky['DNI'],
                                                         clearSky['ExtraI'],
                                                         sp['zenith'],
                                                         sp['azimuth'])

    # Calculate the diffuse radiation from the ground on to the plane of the array
    clearSky['DiffuseGroundI'] = pvlib.irradiance.grounddiffuse(pvobj.tilt,
                                                                clearSky['GHI'])

    # Sum the two diffuse to get total diffuse
    clearSky['DiffuseTotal'] = clearSky['DiffuseGroundI'] + clearSky['DiffuseSkyI']

    # Sum the diffuse and beam temperature to get the total incident irradicance
    clearSky['POA'] = clearSky['DiffuseTotal'] + clearSky['BeamI']

    return clearSky

def calc_clear_index(meas, ub):
    # calculates clear-sky index of meas relative to ub
    # accepts input as pd.Series or np.ndarray
    # returns the same type as the inputs
    clearIndex = calc_ratio(meas, ub)
    clearIndex[clearIndex == np.inf] = 1.0
    clearIndex[np.isnan(clearIndex)] = 1.0
    clearIndex[clearIndex > 1.0] = 1.0

    return clearIndex

    
def calc_ratio(X, Y):

    # Compute the ratio of X to Y
    # inputs X and Y can be pandas.Series, or np.ndarray
    # returns the same type as the inputs
    np.seterr(divide='ignore')  
    ratio = np.true_divide(X, Y)
    np.seterr(divide='raise')
    return ratio

def forecast(pvobj, start, end, deltat, history, order=None,
             dataWindowLength=timedelta(hours=1)):

    """ generate forecast for pvobj from start to end at time resolution deltat
    using data in history.
    Required arguments:
    - pvobj is an instance of class PVobj
    - start is a datetime specifying the time for the first forecast value
    - end is a datetime specifying the time at which the forecast ends. The
    - forecast is provided at times start, start+deltat, start+2*deltat, etc.
    - The last forecast value may not fall at the time given by end.
    - deltat is a timedelta specifying the interval between forecast values.
    - history is a pandas Series with historical values from which the forecast
    - is made.
    
    Optional arguments:
    - dataWindow (type timedelta) length of time window used to fit model, 
      default 1 hour.
    """

    # TODO: input validation

    # align history data with requested forecast period and interval
    # datetime index for history
    target_offset = pd.to_timedelta(deltat)

    # create datetime index for forecast 
    fdr = pd.DatetimeIndex(start=start, end=end, freq=target_offset) 
    # number of intervals with length deltat between start of history and start of forecast
    num_intervals = int(
                 (min(fdr) - min(history.index)).total_seconds() / deltat.seconds)

    # interpolate history to new datetime index idr with interval deltat 
    # and starting in phase with forecast
    idr = pd.DatetimeIndex(start=min(fdr) - num_intervals*deltat,
                           end=end,
                           freq=target_offset)
    tmpdata = pd.DataFrame(index=idr, data=np.nan, columns=['ACpower'])
    # calculates minutes out of phase with midnight
    base = int((idr[0].replace(hour=0, second=0) - idr[0].normalize()).total_seconds()/60)
    
    # merge history with empty dataframe that has the index we want
    newdata = tmpdata.merge(history.to_frame(), 
                            how='outer', 
                            on=['ACpower'], 
                            left_index=True, 
                            right_index=True).tz_convert(tmpdata.index.tz)
    # fill in values on idr timesteps by interpolation
    newdata.interpolate(inplace=True)
    # trim to start at first index in idr (in phase with forecast start), 
    # and don't overrun history
    idata = newdata[(newdata.index>=min(idr)) & (newdata.index<=max(history.index))].copy()
    # want time averages in phase with forecast start over specified deltat.
    idata = idata.resample(target_offset, closed='left', label='left', base=base).mean()
    
    # select data within dataWindowLength
    edatatime = max(idata.index)
    fdatatime = edatatime - dataWindowLength
    fitdata = idata.loc[(idata.index>=fdatatime) & (idata.index<=edatatime)]
    # TODO: model identification logic

    # fit model of order (p, d, q)
    # defaults: 
    if not order:
        if deltat.total_seconds()>=15*60:
            # use MA process
            p = 0
            d = 1
            q = 1
        else:
            # use AR process
            p = 1
            d = 1
            q = 0
        order = (p, d, q)
    
    model = sm.tsa.statespace.SARIMAX(fitdata, 
                                      trend='n', 
                                      order=order)
    results = model.fit()
    
    # determine number of intervals for forecast. start with first interval
    # after the data used to fit the model. +1 because steps counts intervals 
    # we want the interval after the last entry in fdr
    steps = len(idr[idr>max(fitdata.index)]) + 1 
#    f = results.forecast(seasonalPeriod)
    f = results.forecast(steps)
    
    return f[fdr]
    
#    def ARMA_one_step_forecast(self, y, column='Actual', p=1, q=0):
#        """
#        Make a one-step forecast from data in dataframe y. The data in y is differenced once. The default
#        ARMA parameters are p=1, q=0.
#        :param y: pandas dataframe
#        :param column: A valid column name in the dataframe y to forecast
#        :return: f a one-step ahead forecast for y
#        """
#        arima = sm.tsa.ARIMA(y[column], (p, 1, q)).fit()
#        f = arima.forecast(1)
#
#        return f
#
#    def ARMA_seasonal_forecast(self, et, y, windowLength=timedelta(days=7), seasonalPeriod=96,
#                               tf='%Y_%m_%d_%H%M', saveModel=True):
#        """
#
#        :param et: datetime end time for end of the forecast period.
#        :param y:  dataframe object containing at least the same length of data as the window length.
#        :param windowLength: The number of days to include in building the forecast
#        :param seasonalPeriod: The number of lags before the identified season begins.
#        :param tf: time format string for loading/saving model files.
#        :param saveModel: bool flag for saving model seasonal ARMA model results.
#        :return: pandas dataframe containig forecast for the next i points after et in y.
#        """
#        while True:
#            try:
#                st = et - windowLength
#                d = y.loc[st:et]
#                d.to_csv('C:\\python\\forecasting\\d_temp.csv')
#                rel = 'ARMA_model_files\\'
#                fname = rel + st.strftime(tf) + '_to_' + et.strftime(tf) + '.pickle'
#                model = sm.tsa.statespace.SARIMAX(d['Actual'], trend='n', order=(0, 1, 0),
#                                                  seasonal_order=(1, 1, 1, seasonalPeriod))
#                if os.path.isfile(fname):
#                    results = sm.load(fname)
#                else:
#                    results = model.fit()
#
#                if saveModel:
#                    results.save(fname)
#
#                f = results.forecast(seasonalPeriod)
#                break  # will break while true when stable forecast has been made.
#            except Exception as e:
#                print(e)
#                print('SHORT FORECAST: Calculating new parameters...')
#                windowLength = windowLength - timedelta(days=1)
#        return f
#
#    def ARMA(self, y, st, et, uptodate):
#        """
#
#        :param y: pandas dataframe. Historical data to use for forecasting.
#        :param starttime: datetime. The time to begin forecasting at.
#        :param endtime:  datetime. Time to end forecasting and return
#        :param uptodate: bool. Indicating the historical file is up to date with current time.
#        :return: pandas dataframe. Data frame with updated forecast and actual
#        """
#
#        # constants
#        utc = pytz.utc
#        mountain = pytz.timezone('US/Mountain')
#        # tag = 'Meter_PV REC KW'  # tag for the pi data
#        # col = ['datetime_utc', 'Actual']  # column names for dataframe
#        forecast = 'ACPowerForecast'
#
#        # update forecast
#        ptr = st
#        while ptr != et + timedelta(minutes=15):
#            w = y.loc[ptr - timedelta(hours=1, minutes=45):ptr]
#            try:
#                f = self.ARMA_one_step_forecast(w)
#                if f[0] < 0:
#                    y.loc[ptr + timedelta(minutes=15), forecast] = 0
#                else:
#                    y.loc[ptr + timedelta(minutes=15), forecast] = f[0]
#            except Exception as e:
#                y.loc[ptr + timedelta(minutes=15), forecast] = 0
##                print(ptr)
##                print(e)
#                pass
#
#            # step forward
#            ptr = ptr + timedelta(minutes=15)
#
#        # Add the latest forecast datetime column
#        y.loc[ptr, 'datetime_utc'] = ptr.strftime('%m/%d/%Y %H:%M:%S')
#
#        # fill the forecast column with the rest of the previous day-ahead seasonal arma forecast.
#        if not uptodate:
#            curTime = self.checkTime()
#            localTime = curTime.astimezone(mountain)
#
#            yesterdayMidnight = datetime(year=localTime.year, month=localTime.month, day=localTime.day,
#                                         hour=0, minute=0, second=0, microsecond=0, tzinfo=mountain)
#
#            f = self.ARMA_seasonal_forecast(yesterdayMidnight, y, seasonalPeriod=96)
#            f = f.to_frame('ACPowerForecast')
#            f.loc[f['ACPowerForecast'] < 5] = 0
#            f['datetime_utc'] = f.index.strftime('%m/%d/%Y %H:%M:%S')
#            y = y.combine_first(f.loc[max(y.index) + timedelta(minutes=15):])
#
#        return y
#
#    def ClearSkyUpperBoundForecast(self, dr, m, SiteInformation, ModuleParameters, Inverter, NumInv):
#        """
#        Creates clear sky upper bound forecast from measured and clear sky data.
#
#        :param m: dataframe of measured weather station temperature and wind speed
#        :param SiteInformation: dictionary['latitude', 'longitude', 'tz', 'altitude']
#        :param ModuleParameters: pvlib pv system
#        :param Inverter: pvlib inverter from inverter database
#        :param NumInv: number of inverters
#        :return: dataframe 'ACPowerForecast' column
#        """
#
#        # calculate solar position (sp) and clearsky irradiance (cs) for date range dr
#        cs, sp = self.ClearSkyModel(dr, SiteInformation)
#
#        # transfer values of temperature and windspeed from input m to dataframe cs
#        Ta = np.array(m['Temperature'].values)
#        WS = np.array(m['WindSpeed'].values)
#        while len(Ta)<len(dr):
#            Ta = np.concatenate((Ta, m['Temperature'].values))
#            WS = np.concatenate((WS, m['WindSpeed'].values))
#        Ta = Ta[0:len(dr)]
#        WS = WS[0:len(dr)]
#        cs['Temperature'] = Ta
#        cs['WindSpeed'] = WS
#
#        cs = cs.combine_first(sp)
#        f = IrradtoPower(cs, SiteInformation, ModuleParameters, Inverter, NumInv)
#        f['CSACPowerForecast'] = f['ACPowerForecast']
#
#        tf = [x.strftime('%m/%d/%Y %H:%M:%S') for x in f.index]
#        f['datetime_utc'] = tf
#
#        ## Adjust ac power forecast for clear index
#        # This will enforce the clear index to one when dividing measured/forecast to be 1
#        # cs.loc[sp['zenith'] > 85, 'ACPowerForecast'] = m.loc[sp['zenith'] > 85, 'Actual']
#
#        # Adjust the clear sky ac power forecast for prediction
#        # write in 0 for dark hours
#        f.loc[sp['zenith'] >= 90, 'CSACPowerForecast'] = 0
#        # replace time interval near sunrise and sunset with linear interpolation
#        f.loc[(sp['zenith'] > 85) & (sp['zenith'] < 90), 'CSACPowerForecast'] = float('nan')
#        # Ensure the beginning of the interval is not nan to allow interpolation
#        if pd.isnull(f.loc[min(dr), 'CSACPowerForecast']):
#            f.loc[min(dr), 'CSACPowerForecast'] = 0
#        f = f.interpolate(method='time')
#        # remove negative values that occur at very low irradiance levels
#        f.loc[f['CSACPowerForecast'] < 0, 'CSACPowerForecast'] = 0
#        f = f[['datetime_utc', 'CSACPowerForecast']]
#        # return f[['ACPowerForecast', 'CSACPowerForecast']]
#        return f
#
#
#    def calc_clearsky_index(self, meas, ub):
#
#        # Compute the clear index as a ratio of meas to ub
#        # returns a np.array
#
#        try:
#            clearIndex = np.true_divide(meas['Actual'].values, ub['CSACPowerForecast'].values)
#        except RuntimeWarning:
#            print('SHORT FORECAST: RuntimeWarning - likely dividing by zero.')
#            clearIndex = 0
#        clearIndex[clearIndex == np.inf] = 1.0
#        clearIndex[np.isnan(clearIndex)] = 1.0
#        clearIndex[clearIndex > 1.0] = 1.0
#
#        ci = pd.DataFrame(index=meas.index, columns=['clearIndex'])
#        ci['clearIndex'] = clearIndex
#
#        return np.array(ci['clearIndex'].values)
#
#    def compile_kt(self, dr, ci, sr_ss_window=timedelta(hours=1.5)):
#        
#        # returns a dataframe containing kt values using dr as the index.
#        # dr is a pandas Datetimeindex
#        # ci is a list of values to repeat to form kt
#        # sr_ss_window is a timedelta. For this period after sunrise, or before
#        # sunset, kt values are overwritten by 1
#        
#        if not dr.freq:
#            if len(dr)>3:
#                tmpfreq = pd.infer_freq(dr)
#            elif len(dr)==2:
#                tmpfreq = dr[1] - dr[0]
#            else:
#                tmpfreq = timedelta(minutes=15) # default time step
#        else:
#            tmpfreq = dr.freq
#            
#        kt = np.array([])
#        # make sure kt is same length as period to be forecast
#        while len(kt) < len(dr):
#            kt = np.concatenate((kt, ci))
#        kt = kt[0:len(dr)]
#
#        df = pd.DataFrame(index=dr)
#        df['kt'] = kt
#
#        # overwrite sunrise/sunset hours with clear sky model
#        # extend dr each end in case first/last values are within a sunrise/sunset window
#        tdr = dr.union(pd.DatetimeIndex(start=min(dr - sr_ss_window), end=min(dr), freq = tmpfreq))
#        tdr = tdr.union(pd.DatetimeIndex(start=max(tdr), end=max(dr + sr_ss_window), freq = tmpfreq))
#
#        # find sunrise/sunset hours
#        sp = pvlib.solarposition.ephemeris(tdr, self.siteInfo['latitude'], self.siteInfo['longitude'])
#        dl = sp['zenith'].values<90
#        sr = dl[1:] & (dl[:-1] != dl[1:])
#        sr = np.append(False, sr)
#        ss = dl[:-1] & (dl[:-1] != dl[1:])
#        ss = np.append(ss, False)
#        sridx = sp.index[sr]
#        ssidx = sp.index[ss]
#        df = df.combine_first(pd.DataFrame(index=tdr))
#        for i in sridx:
#            u = (tdr > i) & (tdr < i + sr_ss_window)
#            df.loc[tdr[u], 'kt'] = 1
#        for i in ssidx:
#            u = (tdr < i) & (tdr > i - sr_ss_window)
#            df.loc[tdr[u], 'kt'] = 1
#
#        return df
#        
#    def PersistenceForecast(self, y, kt, dr, f_upperbound):
#        # generates a forecast for times in dr using clearsky index in kt and f_upperbound.
#        # Data in kt are tiled to fill the length
#        # of the requested forecast dr.  Clearness index is multiplied by f_upperbound
#        # to produce the forecast, so it is implicit that f_upperbound covers the period
#        # specified by dr.
#        # returns dataframe 'y' which accumulates the history of forecast and actual values
#
#
#        f_upper = f_upperbound.loc[dr, ['datetime_utc', 'CSACPowerForecast']]
#        
#        # add kt column to f_upper
#        tmp = self.compile_kt(dr, kt)
#        
#        # Now multiply
#        f_upper['ACPowerForecast'] = f_upper['CSACPowerForecast'] * tmp['kt']
#        # extend y with new index values
#        y = y.combine_first(pd.DataFrame(index=dr))
#        # overwrite forecast values for date range dr
#        y.loc[dr, ['ACPowerForecast', 'datetime_utc']] = f_upper.loc[dr, ['ACPowerForecast', 'datetime_utc']]
#
#        return y

if __name__ == "__main__":

    USMtn = pytz.timezone('US/Mountain')
    if pvlib.__version__ < '0.4.1':
        print('pvlib out-of-date, found version ' + pvlib.__version__ +
              ', please upgrade to 0.4.1 or later')
    else:
        # make a dict of PV system objects 
        pvdict = {};
        pvdict['Prosperity'] = PVobj('Prosperity', 
                                     dc_capacity=500,
                                     ac_capacity=480,
                                     lat=35.04,
                                     lon=-106.62,
                                     alt=1619,
                                     tz=USMtn,
                                     tilt=25,
                                     azimuth=180,
                                     forecast_method='ARMA')
        pvdict['Prosperityx2'] = PVobj('Prosperity', 
                                     dc_capacity=1000,
                                     ac_capacity=960,
                                     lat=35.04,
                                     lon=-106.62,
                                     alt=1619,
                                     tz=USMtn,
                                     tilt=35,
                                     azimuth=240,
                                     forecast_method='ARMA')

        plt.plot(pvdict['Prosperity'].clearskypower['ACpower'][:1440])
        plt.show()
        
        plt.plot(pvdict['Prosperityx2'].clearskypower['ACpower'][:1440])
        plt.show()
        
        pvobj = pvdict['Prosperity']
        hstart = datetime(2016, 1, 3, 0, 0, 0, tzinfo=USMtn)
        hend = datetime(2016, 1, 6, 11, 50, 0, tzinfo=USMtn)
        dat = pvobj.clearskypower['ACpower']
        history = dat.loc[(dat.index>=hstart) & (dat.index<hend)]
        fstart = datetime(2016, 1, 6, 12, 3, 0, tzinfo=USMtn)
        fend = fstart + timedelta(minutes=60)
        fcst = forecast(pvobj, 
                        start=fstart,
                        end=fend,
                        deltat=timedelta(minutes=15),
                        history=history,
                        order=(1, 1, 0),
                        dataWindowLength=timedelta(hours=2))
        print(fcst)
        
        # for plotting
        hst = history[history.index>=fstart - timedelta(hours=3)]
        dateFormatter = mdates.DateFormatter('%H:%M')
        plt.gca().xaxis.set(major_formatter=dateFormatter)
        plt.xticks(rotation=70)
        plt.plot(hst, 'b-')
        plt.plot(fcst, 'r*')
        plt.show()