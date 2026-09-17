let

    Source = try

        let

            _src = Oracle.Database("oracle.example.com:1521/MFGDB"),

            _nav = _src{[Schema="DBO", Item="Maintenance"]}[Data]

        in _nav

    otherwise

        #table({"maint_id", "machine_id", "maint_date", "maint_type", "maint_cost", "downtime_hours", "technician", "description"}, {}),
    #"Renamed Columns" = Table.RenameColumns(Source, {{"maint_id", "Maint ID"}, {"machine_id", "Machine ID"}, {"maint_date", "Maint Date"}, {"maint_type", "Maint Type"}, {"maint_cost", "Maint Cost"}, {"downtime_hours", "Downtime Hours"}, {"technician", "Technician"}, {"description", "Description"}}),
    Result = #"Renamed Columns"
in
    Result