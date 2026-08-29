import kicad_sch_api as ksa

sch = ksa.create_schematic("MAX2659 GNSS LNA Front End")

l1 = sch.components.add('Device:L', 'L1', '6.8nH', position=(20, 50))
l1.rotate(90)

c1 = sch.components.add('Device:C', 'C1', '100pF', position=(40, 50))
c1.rotate(90)

u1 = sch.components.add('RF_Amplifier:MAX2679B', 'U1', 'MAX2659 (see BOM note)', position=(70, 50))

c2 = sch.components.add('Device:C', 'C2', '100nF', position=(90, 30))
vcc = sch.components.add('power:VCC', 'VCC1', position=(90, 15))
gnd_amp = sch.components.add('power:GND', 'GND1', position=(67.31, 70))
flg1 = sch.components.add('power:PWR_FLAG', 'FLG1', position=(54.61, 70))
gnd_cap = sch.components.add('power:GND', 'GND2', position=(100, 40))

print("Real pin positions after rotation:")
for ref in ['L1', 'C1', 'U1']:
    print(f"  {ref}: {sch.list_component_pins(ref)}")

print("\nRouting the horizontal input chain...")
sch.auto_route_pins('L1', '2', 'C1', '1', routing_strategy='manhattan')
sch.auto_route_pins('C1', '2', 'U1', 'B1', routing_strategy='manhattan')

print("Routing power connections...")
sch.auto_route_pins('U1', 'B2', 'GND1', '1', routing_strategy='manhattan')
sch.auto_route_pins('FLG1', '1', 'GND1', '1', routing_strategy='manhattan')
sch.auto_route_pins('U1', 'A1', 'VCC1', '1', routing_strategy='manhattan')
sch.auto_route_pins('VCC1', '1', 'C2', '1', routing_strategy='manhattan')
sch.auto_route_pins('C2', '2', 'GND2', '1', routing_strategy='manhattan')

sch.add_label("ANT_IN", position=sch.get_component_pin_position('L1', '1'))
sch.add_label("RF_OUT", position=sch.get_component_pin_position('U1', 'A2'))

sch.save("max2659_frontend.kicad_sch")
erc_result = sch.run_erc()
print(f"\nErrors: {erc_result.error_count}, Warnings: {erc_result.warning_count}")