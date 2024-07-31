import time
import sys
import pandas as pd
from multiprocessing import Process, Queue, Event



class GetIPUPower(object):   
    
    def __enter__(self):
        self.end_event = Event()
        self.power_queue = Queue()
        
        interval = 100 #ms
        self.smip = Process(target=self._power_loop,
                args=(self.power_queue, self.end_event, interval))
        self.smip.start()
        return self


    def pow_to_float(self,pow):
        # Power is reported in the format xxx.xxW, so remove the last character.
        # We also handle the case when the power reports as N/A.
        try:
            return float(pow[:-1])
        except ValueError:
            return 0
    
    def _power_loop(self,queue, event, interval):
        import gcipuinfo
        ipu_info = gcipuinfo.gcipuinfo()
        num_devices = len(ipu_info.getDevices())
        
        power_value_dict = {
            idx : [] for idx in range(num_devices)
        }
        power_value_dict['timestamps'] = []
       
        last_timestamp = time.time()

        while not event.is_set():
            #for idx in range(num_devices):
            gcipuinfo.IpuPower
            device_powers=ipu_info.getNamedAttributeForAll(gcipuinfo.IpuPower)
            device_powers = [self.pow_to_float(pow) for pow in device_powers if pow != "N/A"]
            for idx in range(num_devices):
                power_value_dict[idx].append(device_powers[idx])
            timestamp = time.time()
            power_value_dict['timestamps'].append(timestamp)
            wait_for = max(0,1e-3*interval-(timestamp-last_timestamp))
            time.sleep(wait_for)
            last_timestamp = timestamp
        queue.put(power_value_dict)

    def __exit__(self, type, value, traceback):
        self.end_event.set()
        power_value_dict = self.power_queue.get()
        self.smip.join()

        self.df = pd.DataFrame(power_value_dict)
        
    def energy(self):
        import numpy as np
        _energy = []
        energy_df = self.df.loc[:,self.df.columns != 'timestamps'].astype(float).multiply(self.df["timestamps"].diff(),axis="index")/3600
        _energy = energy_df[1:].sum(axis=0).values.tolist()
        return _energy


    