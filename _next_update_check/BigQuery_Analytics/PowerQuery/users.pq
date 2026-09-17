let

    Source = try

        let

            _src = GoogleBigQuery.Database([BillingProject="example-analytics-project"]),

            _nav = _src{[Name="web_data"]}[Data]

        in _nav

    otherwise

        #table({"user_id", "user_name", "email", "signup_date", "plan", "region"}, {}),
    #"users Table" = Source{[Name="users"]}[Data],
    Result = #"users Table"
in
    Result