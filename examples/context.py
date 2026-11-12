import json
from pathlib import Path

def load():
    return json.loads((Path(__file__).with_name("context.json")).read_text())

def run():
    from energyflow.service import EnergyService
    from decimal import Decimal
    from energyflow.values import fraction
    data = load()
    with EnergyService() as app:
        count = 0
        for meter,series in data['series'].items():
            app.catalog.register(meter,'合成车间电表')
            app.catalog.install('counter-'+meter,meter,0,0,max_gap=3600)
            total = Decimal(0)
            readings=[]
            for i,energy in enumerate(series,1):
                total += Decimal(energy)
                readings.append({'id':f'{meter}-{i}','segment':'counter-'+meter,'at':i*data['grid_seconds'],'value':int(total)})
            app.readings.import_batch('day-'+meter,readings)
            profile = app.reconstruction.reconstruct(meter,0,len(series)*data['grid_seconds'])
            assert profile['complete']
            assert fraction(profile['microkwh']) == int(total) * 1000000
            count += len(readings)
        for tariff in data['tariffs']:
            app.tariffs.put(tariff['id'],'main',tariff['start'],tariff['end'],tariff['price'])
        return {'ok': app.quality.system()['ok'], 'records': count}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
